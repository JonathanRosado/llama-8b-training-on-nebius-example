#!/bin/bash
#SBATCH --job-name=cluster-validator
#SBATCH --output=/home/nebius/validator-reports/validator_%j.out
#SBATCH --error=/home/nebius/validator-reports/validator_%j.out
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=8
#SBATCH --gpus-per-node=8
#SBATCH --cpus-per-task=16
#SBATCH --mem=1280G
#SBATCH --time=00:30:00
#SBATCH --export=ALL

# Path B (Soperator + InfiniBand) validator wrapper.
#
# Differs from validator/validate.sbatch (Path A, Ethernet) in:
#   - SLURM_CLUSTER_HAS_IB=1 enables IB-specific checks (nccl_ib_isolated,
#     ib_bandwidth) and raises the multi-node NCCL threshold.
#   - NCCL_INTER_BUSBW_GBPS_MIN=40 reflects the canonical NDR400 IB floor.
#   - STORAGE_PATH=/home/nebius for Soperator's virtiofs-mounted jail.
#   - REPORT_DIR lives under the jail so both login and worker pods see it.
#
# Submit from the login pod:
#   sbatch /home/nebius/validator/validate.sbatch

export SLURM_CLUSTER_HAS_IB=1
export NCCL_INTER_BUSBW_GBPS_MIN="${NCCL_INTER_BUSBW_GBPS_MIN:-40}"

export STORAGE_PATH=/home/nebius
export CONTAINER_IMAGE="${CONTAINER_IMAGE:-nvcr.io/nvidia/hpc-benchmarks:26.02}"
export PYTORCH_CONTAINER_IMAGE="${PYTORCH_CONTAINER_IMAGE:-nvcr.io/nvidia/pytorch:24.12-py3}"
export STORAGE_CONTAINER_IMAGE="${STORAGE_CONTAINER_IMAGE:-$CONTAINER_IMAGE}"

export REPORT_DIR="${REPORT_DIR:-/home/nebius/validator-reports/${SLURM_JOB_ID:-manual}}"
mkdir -p "$REPORT_DIR"
cd "$REPORT_DIR"

# Path B: training scripts live on the Soperator virtiofs jail at
# /home/nebius/... (the jail-relative path slurmd sees after its chroot).
# We bind-mount /home/nebius into pyxis/enroot containers so torchrun can
# read the script. Unlike Path A we do NOT mount /nfs/home or /nfs/shared
# because the jail is the shared filesystem on Path B.
export TRAINING_SCRIPT_DIR=/home/nebius/training
export VALIDATOR_EXTRA_MOUNTS=/home/nebius
# Soperator (Path B): Soperator's slurmd plugstack.conf loads chroot.so
# before spank_pyxis.so, and that ordering breaks enroot's 98-nvidia.sh
# injection for nested pyxis containers (libnvidia-ml.so + /dev/nvidia0..7
# don't land in the container rootfs). We bypass pyxis for the torch-based
# NCCL checks and run torchrun directly from a jail-resident venv that has
# PyTorch installed. The slurmd pod already has a working NVIDIA userspace
# visible from the jail, so torch.cuda works natively here.
export VALIDATOR_TORCHRUN=/home/nebius/venv/bin/torchrun

python3 /home/nebius/validator/validate.py
