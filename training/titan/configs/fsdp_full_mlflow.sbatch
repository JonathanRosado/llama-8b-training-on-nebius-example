#!/bin/bash
#SBATCH --job-name=fsdp_full_mlflow
#SBATCH --output=/home/nebius/training-runs/fsdp_full_mlflow/%j/slurm.out
#SBATCH --error=/home/nebius/training-runs/fsdp_full_mlflow/%j/slurm.out
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=8
#SBATCH --cpus-per-task=16
#SBATCH --mem=0
#SBATCH --time=00:20:00
#SBATCH --export=ALL
set -euo pipefail
JOBID="${SLURM_JOB_ID:-manual}"
MASTER_ADDR=$(scontrol show hostnames "${SLURM_JOB_NODELIST}" | head -n 1)
MASTER_PORT=29500
RUN_DIR="/home/nebius/training-runs/fsdp_full_mlflow/${JOBID}"
mkdir -p "${RUN_DIR}/metrics"

unset NCCL_IB_DISABLE NCCL_SOCKET_IFNAME
export NCCL_IB_HCA=mlx5
export NCCL_IB_GID_INDEX=3
export TORCH_NCCL_ASYNC_ERROR_HANDLING=1
export LOG_RANK=0
export MLFLOW_TRACKING_URI=http://mlflow-tracking.mlflow.svc.cluster.local
export MLFLOW_EXPERIMENT_NAME=torchtitan-fsdp-full
export MLFLOW_RUN_NAME="fsdp_full_job${JOBID}"
# Nebius ml-flow chart enables basic-auth. Submitter must set
# MLFLOW_TRACKING_USERNAME / MLFLOW_TRACKING_PASSWORD before `sbatch`; creds
# at ~/.config/nebius-takehome/mlflow_creds per CLAUDE.md.
: "${MLFLOW_TRACKING_USERNAME:?set MLFLOW_TRACKING_USERNAME before sbatch}"
: "${MLFLOW_TRACKING_PASSWORD:?set MLFLOW_TRACKING_PASSWORD before sbatch}"
export JOBID MASTER_ADDR MASTER_PORT

cd /home/nebius/torchtitan
srun bash -lc '
set -euo pipefail
cd /home/nebius/torchtitan
/home/nebius/venv/bin/torchrun \
  --nnodes=2 --nproc-per-node=8 \
  --rdzv-backend=c10d --rdzv-id=${JOBID} --rdzv-endpoint=${MASTER_ADDR}:${MASTER_PORT} \
  /home/nebius/torchtitan/mlflow_wrapper.py \
  -- \
  --module llama3 --config llama3_8b \
  --hf_assets_path /home/nebius/.cache/huggingface/assets/Llama-3.1-8B \
  --training.steps 60 --training.seq_len 4096 --training.local_batch_size 1 --training.dtype bfloat16 \
  --parallelism.data_parallel_shard_degree -1 \
  --parallelism.tensor_parallel_degree 1 \
  --parallelism.pipeline_parallel_degree 1 \
  --metrics.enable_tensorboard --metrics.log_freq 5 \
  --dump_folder '"'${RUN_DIR}'"'
'
