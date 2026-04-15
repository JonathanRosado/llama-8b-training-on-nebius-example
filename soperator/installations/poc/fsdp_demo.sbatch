#!/bin/bash
#SBATCH --job-name=fsdp-demo-ib
#SBATCH --output=/home/nebius/checkpoints/fsdp_demo/%j/slurm.out
#SBATCH --error=/home/nebius/checkpoints/fsdp_demo/%j/slurm.out
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=8
#SBATCH --cpus-per-task=32
#SBATCH --mem=0
#SBATCH --time=00:15:00
#SBATCH --export=ALL

# FSDP demo — Path B (Soperator + InfiniBand) variant.
#
# Differs from training/sbatch/fsdp_demo.sbatch (Path A Ethernet) in:
#   - NCCL_IB_DISABLE unset (allow IB transport)
#   - NCCL_IB_HCA=mlx5 auto-selects all Mellanox HCAs on the node
#   - NCCL_IB_GID_INDEX=3 for RoCEv2-style GID selection (Nebius default)
#   - No NCCL_SOCKET_IFNAME (sockets are fallback, not primary)
#   - Jail paths (/mnt/jail/...) instead of /nfs/... because Soperator's
#     virtiofs jail is the shared namespace visible from every worker.
#
# Purpose: prove distributed training works end-to-end on the
# Soperator + InfiniBand stack. Same 16-rank (2 nodes × 8 H200)
# FSDP-sharded transformer — demonstrates IB-era tokens/sec/rank
# relative to Path A's Ethernet number.

JOBID="${SLURM_JOB_ID:-manual}"
CKPT_DIR="/home/nebius/checkpoints/fsdp_demo/${JOBID}"
mkdir -p "${CKPT_DIR}"
cd "${CKPT_DIR}"

MASTER_ADDR=$(scontrol show hostnames "${SLURM_JOB_NODELIST}" | head -n 1)
MASTER_PORT=29500

# IB NCCL env — canonical upstream tuning for NDR400 8-HCA H200 nodes.
# NCCL_IB_HCA=mlx5 matches every HCA whose name starts with "mlx5" so
# NCCL uses all 8 HCAs per node (one per GPU). NCCL_IB_GID_INDEX=3 is
# the Nebius-documented index for the IB fabric GID table. We deliberately
# do NOT set NCCL_SOCKET_IFNAME — the IB path is primary; if NCCL needs
# a socket bootstrap it picks the default route interface.
unset NCCL_IB_DISABLE
unset NCCL_SOCKET_IFNAME
unset NCCL_SOCKET_NTHREADS
unset NCCL_NSOCKS_PERTHREAD
export NCCL_IB_HCA=mlx5
export NCCL_IB_GID_INDEX=3
export TORCH_NCCL_ASYNC_ERROR_HANDLING=1
export CHECKPOINT_DIR="${CKPT_DIR}"

# Path B: bypass pyxis — Soperator's chroot.so × spank_pyxis.so plugstack
# ordering breaks enroot's 98-nvidia.sh injection, so libcuda.so + /dev/nvidia0..7
# don't land inside pyxis containers. Instead we exec the jail-resident venv's
# torchrun directly. The slurmd pod already has a working NVIDIA userspace
# visible from the jail, so torch.cuda works natively.
TORCHRUN=/home/nebius/venv/bin/torchrun

srun bash -c "${TORCHRUN} \
    --nnodes=${SLURM_JOB_NUM_NODES} \
    --nproc-per-node=8 \
    --rdzv-id=${JOBID} \
    --rdzv-backend=c10d \
    --rdzv-endpoint=${MASTER_ADDR}:${MASTER_PORT} \
    /home/nebius/training/fsdp_demo.py \
        --steps=30 \
        --batch-size=4 \
        --seq-len=512 \
        --checkpoint-dir=${CKPT_DIR}"
