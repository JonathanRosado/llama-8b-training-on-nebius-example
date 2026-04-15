#!/bin/bash
#SBATCH --job-name=fsdp-demo
#SBATCH --output=/nfs/shared/checkpoints/fsdp_demo/%j/slurm.out
#SBATCH --error=/nfs/shared/checkpoints/fsdp_demo/%j/slurm.out
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=8
#SBATCH --cpus-per-task=32
#SBATCH --mem=0
#SBATCH --time=00:15:00
#SBATCH --export=ALL

# FSDP demo training job — Path A cluster validation.
#
# Path A is the zero-approvals baseline: no InfiniBand fabric, NCCL runs
# over 200 Gbps Ethernet via sockets. Expected per-step throughput will
# be lower than the IB-equipped Path B variant (see git tag
# path-a-ib-working-v1 for the IB numbers).
#
# Purpose: prove distributed training works end-to-end on the constrained
# Slurm+Slinky+NCCL-over-Ethernet stack by running a tiny FSDP-sharded
# transformer across 2 nodes × 8 H200 GPUs = 16 ranks total.
#
# Exit criteria:
#   - srun step exits 0
#   - loss decreases overall (first_loss > last_loss, last10 net decrease)
#   - /nfs/shared/checkpoints/fsdp_demo/<jobid>/metrics.json + sharded
#     model.pt files exist and are non-empty

JOBID="${SLURM_JOB_ID:-manual}"
CKPT_DIR="/nfs/shared/checkpoints/fsdp_demo/${JOBID}"
mkdir -p "${CKPT_DIR}"
cd "${CKPT_DIR}"

# WHY (container image): nvcr.io/nvidia/pytorch:24.12-py3 ships with
# CUDA 12.6, PyTorch 2.x, and NCCL. The HPC-X OMPI+UCX stack in the
# image is unused on Path A — torch.distributed drives NCCL directly.
CONTAINER_IMAGE="nvcr.io/nvidia/pytorch:24.12-py3"

# Rendezvous address = rank-0 host from Slurm's node list.
MASTER_ADDR=$(scontrol show hostnames "${SLURM_JOB_NODELIST}" | head -n 1)
MASTER_PORT=29500

# WHY (compute,utility): PyTorch needs libcuda.so from the host (compute),
# nvidia-smi + libnvidia-ml.so (utility). utility-only works for the
# nccl-tests binary in hpc-benchmarks (libcuda bundled) but PyTorch's
# torch.cuda init fails with "Found no NVIDIA driver" without compute.
export NVIDIA_DRIVER_CAPABILITIES=compute,utility

# WHY (Ethernet NCCL env): Path A has no InfiniBand fabric, so NCCL
# falls back to sockets. Canonical upstream tuning per
# https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html:
#   - NCCL_IB_DISABLE=1 forces sockets
#   - NCCL_SOCKET_IFNAME==eth0 exact-match syntax (leading '=') picks
#     eth0 specifically so NCCL doesn't probe for non-existent ib*
#   - NCCL_SOCKET_NTHREADS=4 + NCCL_NSOCKS_PERTHREAD=4 give 16 socket
#     threads per rank; recommended for 100G-class+ socket networks.
# TORCH_NCCL_ASYNC_ERROR_HANDLING=1 makes PyTorch abort communicators
# on NCCL error rather than hang — important for long training runs.
export NCCL_IB_DISABLE=1
export NCCL_SOCKET_IFNAME="=eth0"
export NCCL_SOCKET_NTHREADS=4
export NCCL_NSOCKS_PERTHREAD=4
export TORCH_NCCL_ASYNC_ERROR_HANDLING=1
export CHECKPOINT_DIR="${CKPT_DIR}"

# Each slurm task runs torchrun with 8 workers per node. torchrun
# handles rank-to-GPU mapping and PMI-less bootstrap via rdzv.
# --container-mounts maps /nfs/shared for checkpoint writes and the
# training script itself from NFS. /dev/shm is bind-mounted so NCCL's
# intra-node shared-memory transport has the larger Memory-medium
# tmpfs mounted on slurmd (kubelet default of 64 MiB is too small).
srun \
    --container-image="${CONTAINER_IMAGE}" \
    --container-mounts=/nfs/home:/nfs/home,/nfs/shared:/nfs/shared,/dev/shm:/dev/shm \
    bash -c "torchrun \
        --nnodes=${SLURM_JOB_NUM_NODES} \
        --nproc-per-node=8 \
        --rdzv-id=${JOBID} \
        --rdzv-backend=c10d \
        --rdzv-endpoint=${MASTER_ADDR}:${MASTER_PORT} \
        /nfs/home/training/fsdp_demo.py \
            --steps=30 \
            --batch-size=4 \
            --seq-len=512 \
            --checkpoint-dir=${CKPT_DIR}"
