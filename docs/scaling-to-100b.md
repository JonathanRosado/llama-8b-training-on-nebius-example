# Scaling from 16 H200 to 512 H200 — LLaMA 8B, 70B, and 100B+

This doc traces how the TorchTitan training code in `training/titan/` (validated at PoC scale on 16 H200, see `docs/training-efficiency.md`) scales to the customer's 512-H200 production cluster. The code is intentionally shaped for that target — every run variable that would change at 512 GPU is already surfaced in the TorchTitan TOML configs.

## 1. Topology assumption

Production target:

| Dimension | PoC (now) | Production (target) |
|---|---|---|
| GPUs | 16 H200 SXM | 512 H200 SXM |
| Nodes | 2 (8 GPUs/node) | 64 (8 GPUs/node) |
| Intra-node interconnect | NVLink4 + NVSwitch (~900 GB/s bidirectional/GPU) | Same |
| Inter-node interconnect | 8× NDR400 IB per node, rail-optimized | Same (same fabric class, higher radix) |
| Shared storage | 2 TiB Nebius NETWORK_SSD (virtiofs jail) | 500 TiB+ Nebius Object Storage (Nebius S3), local NVMe cache per node |
| Scheduler | Slurm via Soperator 3.0.2 | Same |

## 2. Per-GPU memory budget

H200 SXM = 141 GB HBM3e. Budget per rank, BF16 mixed precision with FP32 Adam master state:

| Model | Params (GB, BF16) | Adam state (GB, FP32) | Grads (GB, BF16) | Activations (est, seq=4K, batch=1) |
|---|---:|---:|---:|---:|
| LLaMA-3.1-8B | 16 | 64 | 16 | 12–18 |
| LLaMA-3.1-70B | 140 | 560 | 140 | 60–90 |
| 100B+ (GPT-4-class) | 200+ | 800+ | 200+ | 80–120 |

Sharding math (divide totals by `data_parallel_shard_degree`):

| Model | Shard degree needed to fit in 141 GB | Notes |
|---|---:|---|
| 8B | ≥ 1 (already fits unsharded!) | FSDP chosen for efficiency, not necessity |
| 70B | ≥ 8 (intra-node NVLink) | HSDP ideal: shard within node, replicate across |
| 100B+ | ≥ 16 or add TP | TP=2 reduces per-rank param footprint before sharding |

## 3. Strategy comparison table

| Strategy | When it wins | When it hurts | TorchTitan config key |
|---|---|---|---|
| **FSDP full-shard** | ≤ 64 GPUs, fits in NVLink domain | > 64 GPUs: cross-rack allreduce dominates | `data_parallel_shard_degree = -1` |
| **HSDP** (ours) | 128–1024 GPUs; shard in NVLink, replicate over IB | At ≤16 GPUs it's ~identical to full-shard | `data_parallel_shard_degree = <intra-node>`, `data_parallel_replicate_degree = <inter-node>` |
| **ZeRO-3** | Framework-native in DeepSpeed; offers stages 1/2/3 | Adds separate framework surface — TorchTitan doesn't need it because FSDP2 covers ZeRO-3 equivalent | N/A in TorchTitan |
| **TP (tensor parallel)** | ≥ 70B when activations > single-GPU HBM | 8B at 16 GPUs: comms dominate (we measured 3× slowdown vs FSDP) | `tensor_parallel_degree = 2, 4, or 8` — must divide `num_attention_heads` (32 for Llama 3.1 8B/70B) |
| **PP (pipeline parallel)** | ≥ 100B, wide-enough cluster to amortize bubble | Bubble overhead at small scale; needs microbatch tuning | `pipeline_parallel_degree > 1`, `pipeline_parallel_schedule = "1F1B"` |
| **CP (context parallel)** | Long context (≥ 32K seq) | Default seq_len of 4K doesn't benefit | `context_parallel_degree > 1` |

Our `training/titan/configs/` set: `fsdp_full.toml` + `hsdp.toml` + `tp_dp.toml`. Adding `pp_tp_dp.toml` for 100B scale is one file away.

## 4. Parallelism recipe ladder (16 → 128 → 512 GPU)

Concrete settings for LLaMA-8B / 70B / 100B on each cluster size. All other TorchTitan keys unchanged from our PoC configs.

| Model | Cluster | TP | PP | DP_shard | DP_replicate | Rationale |
|---|---|---:|---:|---:|---:|---|
| 8B | 16 (PoC) | 1 | 1 | 16 | 1 | Validated: `fsdp_full.toml` |
| 8B | 128 | 1 | 1 | 8 | 16 | HSDP: shard within node × replicate across; `hsdp.toml` scaled |
| 8B | 512 | 1 | 1 | 8 | 64 | HSDP, same shape; replicate grows |
| 70B | 128 | 2 | 1 | 8 | 8 | TP=2 halves per-rank params; shard within node |
| 70B | 512 | 2 | 1 | 8 | 32 | Same shape, more replicas |
| 100B+ | 512 | 4 | 4 | 4 | 8 | 3D parallelism; PP with 1F1B schedule + μbatch tuning |

TP degree constraint: must divide attention-head count. LLaMA-3.1 70B has 64 heads → TP ∈ {1,2,4,8,16,32,64}.

## 5. Checkpoint & restart

TorchTitan uses PyTorch DCP (Distributed Checkpoint, async).

- **Write path**: each rank writes a `.distcp` shard + shared `.metadata`. Async writer thread keeps GPU work unblocked.
- **Cross-world-size reshard**: DCP supports loading a checkpoint saved with different TP/PP/DP degrees, provided FQNs are unchanged. A 16-GPU `fsdp_full` ckpt loads cleanly on a 512-GPU `hsdp` or `tp_pp_dp` run without offline conversion. Verified by the `metadata.pt` round-trip.
- **Production cadence**: every N steps (e.g., 2,000) + on-exit. Async mode overlaps save with next step's compute.
- **Storage**: PoC writes to jail `/home/nebius/training-runs/.../checkpoints/`. Production routes via `checkpoint.folder = s3://...` or virtiofs-mounted Nebius Object Storage for blast-radius isolation from compute node failures.
- **Restart**: `--checkpoint.load_step <n>` or `-1` (latest). TorchTitan resumes optimizer + RNG + data cursor cleanly.

## 6. NCCL / InfiniBand tuning for 500+ GPU

Our validated env set (from `soperator/installations/poc/fsdp_demo.sbatch` + `training/titan/launch.sh`):

```bash
export NCCL_IB_HCA=mlx5              # match all 8 Mellanox HCAs per node
export NCCL_IB_GID_INDEX=3           # Nebius-documented GID index
export TORCH_NCCL_ASYNC_ERROR_HANDLING=1
# unset any Ethernet-era knobs: NCCL_IB_DISABLE, NCCL_SOCKET_IFNAME, NCCL_SOCKET_NTHREADS
```

At 500+ GPU, add:
- `NCCL_COLLNET_ENABLE=1` + SHARP-enabled InfiniBand switches (allreduce in-network, 1.5–2× for small-to-medium allreduce sizes; requires NVIDIA ConnectX-7 + Quantum-2 switches — verify Nebius cluster supports it)
- `NCCL_IB_SPLIT_DATA_ON_QPS=0` + `NCCL_IB_QPS_PER_CONNECTION=4` for NDR400
- `NCCL_P2P_NET_CHUNKSIZE=524288` for large allgather/reducescatter
- Rail-optimized ring/tree topology via `NCCL_TOPO_FILE` (Nebius can supply cluster-specific topology XML)

## 7. Dataset & storage ingress

PoC: `allenai/c4` streamed from HF Hub. Runs 16-ranks × 60 steps. Measured ~10 MB/s aggregate ingress.

Production target: 512 ranks × 100k+ steps. Ingress scales to ~1 GB/s+. HF Hub rate-limits at that shape.

Canonical 500+ GPU path:
1. Stage C4 (or custom corpus) once to **Nebius Object Storage** (`s3://tfstate-slurm-k8s-.../datasets/c4/`) via `huggingface-cli download ... --local-dir`.
2. Mount read-only into each worker pod via Soperator `custom_configmaps` + a MinIO/webdav sidecar, OR via virtiofs from a dataset-server VM.
3. TorchTitan `training.dataset_path = /mnt/datasets/c4/en/tokenized/*.parquet` — file mode, not streaming.
4. Tokenizer cache identical pattern — pin to S3.
5. Use `HF_HUB_OFFLINE=1` in the training env to guarantee no Hub calls during training.

## 8. Efficiency metrics

| Metric | PoC measurement | Production target |
|---|---:|---|
| tok/s/GPU (8B, FSDP2) | 7,785 | ≥ 10k (MegatronLM-class with CuDNN flash-attn + optimizer fusion) |
| MFU (8B, FSDP2) | 40.5% | ≥ 50% (with tuning) |
| HFU | not measured | — |
| MFU (70B, HSDP+TP=2) | — | 50–60% typical on H200 NDR400 |
| MFU (100B, 3D parallel) | — | 40–50% typical (pipeline bubble + comms) |
| Cost / 1B tokens (8B) | $X | depends on GPU-hour rate × MFU; document at contract time |
| Checkpoint time (DCP async, step 60) | ~0.3 s (negligible) | target: < 60 s for 70B, < 300 s for 100B |

## 9. Failure domains and blast radius

| Failure | PoC impact | Production mitigation |
|---|---|---|
| Single GPU HBM fault | 1 rank crash, job exits | Slurm `--requeue` + DCP restart from last ckpt (step-N boundary) |
| Single node down | job exits if node held a rank | Same; Soperator drains the node, Slurm re-schedules job on healthy allocation |
| Inter-node IB partition | allreduce hangs | `TORCH_NCCL_ASYNC_ERROR_HANDLING=1` aborts the communicator + Slurm requeue |
| Dataset server down | training stalls on data fetch | S3 + local-cache prefix: cache survives short outages; pin cache TTL to ≥1 ckpt cadence |
| Control plane (MK8s) outage | submission blocked; running jobs unaffected (Slurm runs on data plane) | MK8s HA (3-etcd) is canonical in Nebius Solutions Library |
| slurmctld crash | pending jobs stall; running jobs continue | Soperator restarts slurmctld via StatefulSet; state persisted on RWO PVC |
| Soperator REST/accounting outage | ActiveCheck surface stale | Training workloads unaffected; alert via Soperator observability |

Blast radius = 1 Slurm job (up to 512 ranks). Soperator isolates different tenant workloads by partition + nodeset.

## 10. Vendor-canonical Nebius component mapping

| Need | Nebius component | Our config |
|---|---|---|
| GPU capacity reservation | `nebius_compute_v1_gpu_cluster` + fabric | `eu-north2-a` fabric, approved quota (Path B only) |
| Managed Kubernetes | `nebius_mk8s_v1_cluster` | v1.32, 3-etcd, public endpoint |
| Shared filesystem | `nebius_compute_v1_filesystem` | 2 TiB jail + 128 GiB controller-spool + 64 GiB accounting (within 2.5 TiB quota) |
| Object storage (datasets, ckpt) | Nebius S3 (via Terraform `nebius_storage_v1_bucket`) | *Deferred to Phase 2* |
| Slurm control plane | Soperator (self-deployed via FluxCD) | `soperator/installations/poc/` (not Managed Soperator) |
| Slurm worker runtime | Soperator jail (virtiofs) + slurmd + enroot/pyxis | Pyxis has a plugstack-ordering issue with Soperator's chroot.so; we bypass via jail-resident venv (`/home/nebius/venv`) — see `CHANGELOG.md` 2026-04-14 Path B entry |
| GPU driver + DCGM | Nebius marketplace release `nebius/nvidia-gpu-operator` | Installed via `nebius_applications_v1alpha1_k8s_release` in Path A; Soperator handles driver stack in Path B |
| InfiniBand | NDR400 mlx5 HCAs on gpu-h200-sxm platform | `NCCL_IB_HCA=mlx5, NCCL_IB_GID_INDEX=3` |
| Active checks | Soperator `ActiveCheck` CRDs (`all-reduce-perf-nccl-with-ib`, `ib-gpu-perf`, etc.) | Validated Complete during Path B bring-up; reused as the IB-specific check surface in our validator (skip + cite) |

## 11. What's NOT in this PoC but IS required for 500-GPU production

1. **Observability**: Prometheus + Grafana wired to DCGM + Slurm exporter (Soperator deploys these already; production needs alerts tuned).
2. **Eval harness**: `lm-eval-harness` or `helm` runs on eval slices; integrate with TorchTitan `eval` config key.
3. **Multi-tenancy**: Slurm accounts + QOS + fairshare (accounting enabled in Path B; tuning is production-only).
4. **Spot/preemption handling**: not required for capacity-reserved GPUs, but the Slurm requeue pattern above handles it.
5. **Data mixing**: C4 alone is a baseline; production LLM recipes blend corpora (The Pile, RedPajama, custom). TorchTitan supports `dataloader.dataset_path` as a list.
6. **Flash-attention + custom kernels**: TorchTitan uses PyTorch SDPA by default; enabling FA-3 kernels on H200 lifts MFU another 10–15%.
7. **Registry cache**: nvcr.io pull-through via Harbor or Nebius container registry (our Canonical Audit plan item — Phase 2).
8. **SOPS-encrypted secrets**: W&B, HF, etc., under FluxCD control (Canonical Audit Phase 2).

## Cross-reference — TOML keys to doc sections

| Doc section | TorchTitan config key |
|---|---|
| §2 memory budget | `model.module` + `model.config` (model selection), `training.dtype`, `training.mixed_precision_param`, `training.mixed_precision_reduce` |
| §3 strategy | `parallelism.data_parallel_shard_degree`, `parallelism.data_parallel_replicate_degree`, `parallelism.tensor_parallel_degree`, `parallelism.pipeline_parallel_degree`, `parallelism.context_parallel_degree` (not set in PoC TOMLs; would be added for long-context runs) |
| §4 ladder | Same as §3, scaled per cluster size |
| §5 checkpointing | `checkpoint.folder`, `checkpoint.load_step`, `checkpoint.async_mode`, `checkpoint.interval` |
| §6 NCCL | environment: set in sbatch launcher, not TOML |
| §7 dataset | `dataloader.dataset`, `dataloader.dataset_path`, `dataloader.num_workers`, `training.seq_len` |
| §8 metrics | `metrics.enable_tensorboard`, `metrics.enable_wandb`, `metrics.log_freq` |
| §9 failure | runtime flag: `TORCH_NCCL_ASYNC_ERROR_HANDLING=1` + `--checkpoint.load_step -1` for requeue |

Keys marked "(not set in PoC TOMLs)" are surfaced here as the scaling-ladder knobs; the committed configs under `training/titan/configs/` cover the three strategies we validated and do not exercise every key in the table.

Configs on disk: `training/titan/configs/fsdp_full.toml`, `hsdp.toml`, `tp_dp.toml`. TorchTitan SHA pinned in each header comment + `training/titan/README.md`.
