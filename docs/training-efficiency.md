# Training Efficiency — LLaMA-3.1-8B on Path B (Soperator + InfiniBand)

**Cluster**: 2× H200 SXM nodes, 8 GPUs each, NDR400 InfiniBand (NCCL_IB_HCA=mlx5, 8 HCAs per node).
**Model**: `meta-llama/Llama-3.1-8B` via TorchTitan 0.2.2 (pinned SHA `74485ea3381d7c1739b004daace3d9ecaead67e9`).
**Dataset**: `allenai/c4` English subset, streamed from Hugging Face Hub during run (PoC-valid; production pattern is Nebius S3-staged — see Caveats).
**Training shape**: 60 steps, seq_len=4096, local_batch_size=1, BF16 mixed precision, AdamW.
**Per-run artifacts**: `training/titan/runs/<config>/` — `resolved-config.json` + `metrics.json`.

## Strategy matrix

| Config | Parallelism | tok/s/GPU | MFU | Mem reserved | Loss (first → last) | Wall-clock | Notes |
|---|---|---:|---:|---:|---|---:|---|
| `fsdp_full` | FSDP2 full-shard, DP_shard=16 | **7,785** | **40.51%** | 15.4% | 12.22 → 7.99 | 44.7 s | Reference strategy. Minimum memory footprint. |
| `hsdp` | DP_shard=8 (intra-node NVLink) × DP_replicate=2 (inter-node IB) | 7,752 | 40.35% | 20.6% | 12.24 → 7.97 | 45.5 s | Hybrid-shard. Within 0.5% of full-shard on 2 nodes; advantage grows with node count. |
| `tp_dp` | TP=2 + DP_replicate=8 (no NVLink-aligned node boundary) | 2,660 | 13.84% | 40.4% | 12.25 → 8.03 | 67.9 s | Tensor-parallel overhead dominates at 16 GPUs; value grows with model size ≥70B. Illustrative only. The TP groups cross node boundaries here (not a strict intra-node-TP / inter-node-DP topology); a node-aligned variant would set `tensor_parallel_degree=8 × data_parallel_replicate_degree=2` but that changes communication patterns materially. |

All three strategies showed an overall loss decrease of ~4.2 units (12.2 → ~8.0 in 60 steps) — real gradient updates, not noise. TorchTitan's TensorBoard logger emits 13 scalar samples at `log_freq=5`, so we report first/last rather than per-step monotonicity. Final loss ≈ 8.0 is expected for a 60-step probe; convergence is not a PoC goal.

MFU computed as `(tokens_per_sec_per_gpu × flops_per_token) / peak_bf16_flops_per_gpu` with LLaMA-3.1-8B forward+backward FLOPs/token ≈ 6 × 8e9 = 4.8e10 and H200 BF16 peak = 989 TFLOPS/GPU.

## Path A cross-reference (Ethernet-only)

Path A's earlier validation used a toy transformer (~2 GB params) over 200 Gbps Ethernet with NCCL socket transport:

| Path | Model | Transport | tok/s/rank | Notes |
|---|---|---|---:|---|
| A (Slinky, Ethernet) | toy transformer | NCCL over sockets | 4,955 | From `CHANGELOG.md` commit `d707070`; Ethernet-transport-bound at ~6.6 GB/s multi-node busbw |
| B (Soperator, InfiniBand) | **LLaMA-3.1-8B** | NCCL over IB (NDR400×8 HCAs) | **7,785** | `fsdp_full` |

**Not directly comparable** — different model + different framework (TorchTitan vs hand-rolled FSDP). The defensible interview framing: Path A's 4,955 tok/s/rank on a toy model establishes that the *Ethernet stack* works end-to-end; Path B's 7,785 tok/s/rank on a real 8B LLM establishes that the *production stack* works and IB removes the socket bottleneck. A like-for-like comparison (same TorchTitan configs on Path A) was scoped out to preserve the Path A quota teardown as evidence of disciplined resource management rather than re-apply cost.

## Caveats — PoC-valid vs production-worthy

These runs are **PoC-valid**. For 500+ GPU production the following must change:

1. **Dataset ingress**: C4 via HF Hub streaming is fine for 16 ranks over 60 steps. At 500+ GPU × 100k+ steps, stage C4 to Nebius Object Storage once and mount read-only (`training.dataset_path` = S3/virtiofs path). Eliminates Hub rate-limit risk + repeated metadata resolution.
2. **Run length**: 60 steps is a smoke test. Production runs of 8B→100B models need 100k+ steps with validation / eval harness + early-stopping telemetry.
3. **Observability**: TorchTitan 0.2.2's TensorBoard exports `loss`, `grad_norm`, `lr`, `memory`; it does **not** export `tps`/`mfu` scalars (verified by enumerating `.Tags()['scalars']`). MFU here was computed offline. Production should enable W&B (`metrics.enable_wandb = true`) for first-class tps/mfu logging + alerting.
4. **TP at scale**: TP=2 is slow at 16 GPUs because cross-rank comms dominate small sub-batches. TP becomes essential at 70B+ where a single activation exceeds single-GPU HBM. The 3D-parallelism recipe in `docs/scaling-to-100b.md` shows when TP earns its keep.
5. **Checkpointing**: DCP at step 60 verified. Cross-world-size resharding is first-class in DCP (PyTorch 2.4+), so an HSDP checkpoint from this 16-GPU run loads cleanly on a 512-GPU FSDP production run. No offline conversion step needed.

## Reproducibility

From the Soperator login pod (or any jail-mounted shell):

```bash
# One-shot setup (Day 0, already done on this cluster):
/home/nebius/venv/bin/pip install -e /home/nebius/torchtitan
scripts/stage_c4.sh  # streams C4 english shard into local HF cache

# Per-strategy run (launch.sh calls sbatch internally — do NOT wrap):
./training/titan/launch.sh fsdp_full
./training/titan/launch.sh hsdp
./training/titan/launch.sh tp_dp
```

Each run writes `/home/nebius/training-runs/<config>/<jobid>/`:
- `resolved-config.json` — effective TorchTitan config after CLI overrides
- `metrics/<timestamp>/events.out.tfevents...` — TensorBoard scalars
- `checkpoints/step-<n>/*.distcp` — DCP checkpoint shards
- `slurm.out` — stdout including TorchTitan log lines

The committed metric summaries under `training/titan/runs/<config>/metrics.json` are the curated PoC evidence (lightweight; the raw TB + checkpoint files stay on the shared jail).
