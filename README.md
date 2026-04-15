# Llama-3.1-8B distributed training on Nebius — example

End-to-end Terraform + Soperator/Slurm + TorchTitan example for running multi-node distributed pretraining of `meta-llama/Llama-3.1-8B` on Nebius AI Cloud H200 GPUs. The architecture is parameterized so the same code runs on a 16-GPU PoC and a 512-GPU production cluster.

## What's here

```
.
├── terraform/path-a/                 # Path A: Slinky Slurm-on-K8s, Ethernet-only (no gpu_cluster quota)
├── soperator/installations/poc/      # Path B: self-deployed Soperator + InfiniBand + virtiofs jail
├── soperator/modules/                # Vendored Nebius Solutions Library Soperator modules
├── modules/                          # Vendored Nebius Solutions Library top-level modules (gpu-operator, network-operator, nfs-server)
├── training/titan/                   # TorchTitan-based 3-strategy training matrix (FSDP2 / HSDP / TP+DP)
│   ├── configs/                      # TOML configs per strategy
│   ├── launch.sh                     # canonical sbatch wrapper
│   ├── mlflow_wrapper.py             # MLflow tracking integration
│   ├── stage_c4.py                   # rclone-based C4 staging to Nebius Object Storage
│   ├── c4_parquet_loader.py          # local-Parquet TorchTitan dataset loader
│   └── runs/                         # per-strategy curated run artifacts
├── training/                         # smaller demo + nccl_bench helpers
├── validator/                        # GPU/IB/NCCL/storage cluster validator (run via Slurm)
├── flux/apps/                        # Flux ConfigMaps for validator + training scripts
├── docs/
│   ├── canonical-audit.md            # 4-axis evidence table — vendor-canonical alignment
│   ├── training-efficiency.md        # 3-strategy efficiency matrix on Path B
│   ├── scaling-to-100b.md            # 16 → 128 → 512 GPU recipe ladder
│   └── upstream-issues/              # vendor-gap reproducer templates
├── scripts/                          # deployment helpers (e.g. C4 staging shell)
├── .envrc                            # canonical Nebius SA + S3 backend bootstrap
└── CHANGELOG.md                      # decision history
```

## Quick start

### Path B (production-shape, requires GPU cluster + 2.5 TiB filesystem quota)

```bash
direnv allow                          # provisions SA + S3 backend per .envrc
cd soperator/installations/poc
terraform init
terraform plan
terraform apply
```

When `terraform apply` finishes you have:
- MK8s cluster with 2× H200 nodes on the eu-north2-a InfiniBand fabric
- Self-deployed Soperator via FluxCD (slurmctld, slurmrestd, accounting MariaDB, jail virtiofs, observability stack)
- MLflow marketplace release for experiment tracking (when `mlflow_enabled=true`)
- Mountpoint-S3 CSI driver and a static PV/PVC pointing at `nebius-c4-datasets`

### Stage the dataset

```bash
python3 training/titan/stage_c4.py --target-bucket nebius-c4-datasets --max-bytes 20G
```

This rclone-uploads the first ~20 GiB of `allenai/c4` English Parquet shards to your dataset bucket. The Mountpoint-S3 CSI exposes them read-only at `/mnt/datasets/c4` inside every slurmd worker.

### Run the strategy matrix

```bash
# from the Soperator login pod, in the jail
sbatch /home/nebius/training/titan/launch.sh fsdp_full
sbatch /home/nebius/training/titan/launch.sh hsdp
sbatch /home/nebius/training/titan/launch.sh tp_dp
```

Each job streams metrics to TensorBoard (per-step, jail-local) and MLflow (run registry, model registry, comparison UI).

### Validate the cluster

```bash
sbatch validator/validate.sbatch
```

Produces a structured JSON + Markdown report covering preflight, GPU info, NCCL NVLink/multi-node bandwidth, IB GPUDirect bandwidth, storage cross-node visibility.

## Architecture notes

- **Two paths**: Path A demonstrates a quota-constrained baseline (no `gpu_cluster`, no shared filesystem, NCCL over Ethernet). Path B is the production-shape deployment (InfiniBand, shared filesystem, full Soperator). The same TorchTitan + validator code runs on both via env-var switching.
- **Parameterized for scale**: training configs surface `data_parallel_shard_degree`, `data_parallel_replicate_degree`, `tensor_parallel_degree`, `pipeline_parallel_degree` directly. Moving from 16 → 512 GPUs is a config change, not a code change.
- **Vendor-canonical**: GPU operator via the Nebius marketplace release (not upstream), Soperator from the Solutions Library, Mountpoint-S3 CSI per `docs.nebius.com/object-storage/interfaces/mountpoint-s3`, rclone for dataset staging per `docs.nebius.com/slurm-soperator/storage/download-data`, MLflow via the `ml-flow` marketplace app.
- **Per-rank metrics**: `training/titan/runs/<config>/metrics.json` captures the headline numbers for each strategy on Path B (PoC scale).

## See also

- `docs/canonical-audit.md` — what we keep canonical and where we deviate, axis by axis.
- `docs/training-efficiency.md` — measured PoC numbers for the three strategies (FSDP2 full-shard, HSDP, TP+DP).
- `docs/scaling-to-100b.md` — concrete recipe ladder from 16 H200 to a 512-GPU production cluster, with memory math, NCCL tuning, DCP checkpointing strategy, and Nebius component mapping.
- `docs/upstream-issues/` — reproducer templates for gaps we hit in the vendor stack (pyxis × chroot ordering, slurmrestd accounting gate, jail tooling).
