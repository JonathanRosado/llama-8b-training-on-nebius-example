#!/bin/bash
#SBATCH --job-name=cluster-validator
#SBATCH --output=validator_%j.out
#SBATCH --error=validator_%j.out
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=8
#SBATCH --gpus-per-node=8
#SBATCH --cpus-per-task=16
#SBATCH --mem=1280G
#SBATCH --time=00:30:00
#SBATCH --export=ALL

# GPU Cluster Validator — sbatch wrapper
#
# Path A is the zero-approvals baseline: no InfiniBand fabric, NCCL runs
# over 200 Gbps Ethernet. IB-specific checks (nccl_ib_isolated,
# ib_bandwidth) auto-skip via SLURM_CLUSTER_HAS_IB=0. Path B sets
# SLURM_CLUSTER_HAS_IB=1 in its own wrapper to enable them.
#
# Usage:
#   sbatch validate.sbatch
#   sbatch --nodes=1 validate.sbatch   # single-node only (multi-node checks SKIPPED)
#
# Override thresholds or image via environment:
#   NCCL_NVLINK_BUSBW_GBPS_MIN=400 sbatch validate.sbatch
#   NCCL_INTER_BUSBW_GBPS_MIN=15 sbatch validate.sbatch

# Path A has no IB; the validator skips IB-specific checks accordingly.
export SLURM_CLUSTER_HAS_IB=0

export STORAGE_PATH=/nfs/shared
export CONTAINER_IMAGE="${CONTAINER_IMAGE:-nvcr.io/nvidia/hpc-benchmarks:26.02}"
export STORAGE_CONTAINER_IMAGE="${STORAGE_CONTAINER_IMAGE:-$CONTAINER_IMAGE}"

# WHY (/nfs/home/validator/): the validator Python file is deployed to the
# shared NFS so both the login pod (where the sbatch body runs) and the
# slurmd pods (where srun container steps execute) can see the same file
# without baking a custom image. The earlier /opt/validator/ path does not
# exist in stock Slinky images.
# WHY (REPORT_DIR on /nfs/shared): per-job report directory on shared NFS so
# validator.log, validator_report.json, and validator_report.md are visible
# from the login pod AND persisted across pod restarts.
export REPORT_DIR="${REPORT_DIR:-/nfs/shared/validator-reports/${SLURM_JOB_ID:-manual}}"
mkdir -p "$REPORT_DIR"
cd "$REPORT_DIR"

python3 /nfs/home/validator/validate.py
