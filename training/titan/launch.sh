#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_NAME="${1:?usage: ./launch.sh <config_name> [--print-only]}"
MODE="${2:-submit}"
CONFIG_FILE="${ROOT_DIR}/configs/${CONFIG_NAME}.toml"

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "config not found: ${CONFIG_FILE}" >&2
  exit 1
fi

SBATCH_FILE="$(mktemp "/tmp/${CONFIG_NAME}.XXXXXX.sbatch")"
trap 'rm -f "${SBATCH_FILE}"' EXIT

python3 - "${CONFIG_FILE}" > "${SBATCH_FILE}" <<'PY'
import json
import shlex
import sys
import tomllib


def shell_join(parts):
    return " ".join(shlex.quote(str(part)) for part in parts)


def bool_flag(path, value):
    head, tail = path.rsplit(".", 1)
    return f"--{head}.{tail}" if value else f"--{head}.no_{tail}"


def flatten_cli(prefix, data):
    parts = []
    for key, value in data.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            parts.extend(flatten_cli(path, value))
        elif isinstance(value, bool):
            parts.append(bool_flag(path, value))
        elif isinstance(value, list):
            joined = ",".join(str(item) for item in value)
            parts.extend([f"--{path}", joined])
        elif value is None:
            continue
        else:
            parts.extend([f"--{path}", value])
    return parts


with open(sys.argv[1], "rb") as handle:
    cfg = tomllib.load(handle)

module = cfg["module"]
config_name = cfg["config"]
slurm = cfg["slurm"]
paths = cfg["paths"]
env = cfg.get("env", {})

job_name = slurm["job_name"]
nodes = slurm["nodes"]
ntasks_per_node = slurm["ntasks_per_node"]
gpus_per_node = slurm["gpus_per_node"]
cpus_per_task = slurm["cpus_per_task"]
time_limit = slurm["time"]

repo = paths["repo"]
hf_assets_path = paths["hf_assets_path"]
run_root = paths["run_root"]
metrics_dirname = paths.get("metrics_dirname", "metrics")

tt_args = [
    "--module",
    module,
    "--config",
    config_name,
    "--hf_assets_path",
    hf_assets_path,
]

for section in (
    "training",
    "dataloader",
    "parallelism",
    "checkpoint",
    "metrics",
    "profiling",
    "validator",
    "debug",
    "compile",
    "comm",
    "activation_checkpoint",
):
    if section in cfg:
        tt_args.extend(flatten_cli(section, cfg[section]))

tt_command = (
    "/home/nebius/venv/bin/torchrun "
    f"--nnodes={nodes} "
    f"--nproc-per-node={gpus_per_node} "
    "--rdzv-backend=c10d "
    "--rdzv-id=${JOBID} "
    "--rdzv-endpoint=${MASTER_ADDR}:${MASTER_PORT} "
    + shell_join(["-m", "torchtitan.train", *tt_args])
)

env_exports = "\n".join(
    f"export {key}={shlex.quote(str(value))}" for key, value in sorted(env.items())
)

script = f"""#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --output={run_root}/%j/slurm.out
#SBATCH --error={run_root}/%j/slurm.out
#SBATCH --nodes={nodes}
#SBATCH --ntasks-per-node={ntasks_per_node}
#SBATCH --gpus-per-node={gpus_per_node}
#SBATCH --cpus-per-task={cpus_per_task}
#SBATCH --mem=0
#SBATCH --time={time_limit}
#SBATCH --export=ALL

set -euo pipefail

JOBID="${{SLURM_JOB_ID:-manual}}"
MASTER_ADDR=$(scontrol show hostnames "${{SLURM_JOB_NODELIST}}" | head -n 1)
MASTER_PORT=29500
RUN_DIR="{run_root}/${{JOBID}}"
mkdir -p "${{RUN_DIR}}/{metrics_dirname}"
cd {shlex.quote(repo)}

unset NCCL_IB_DISABLE
unset NCCL_SOCKET_IFNAME
{env_exports}

export LOG_RANK=0
export TORCHFT_LIGHTHOUSE=http://localhost:29510
export TITAN_RUN_DIR="${{RUN_DIR}}"
export JOBID MASTER_ADDR MASTER_PORT

srun bash -lc '
set -euo pipefail
cd {shlex.quote(repo)}
{tt_command} \
  --dump_folder "${{TITAN_RUN_DIR}}"
'
"""

print(script)
PY

if [[ "${MODE}" == "--print-only" ]]; then
  cat "${SBATCH_FILE}"
  exit 0
fi

sbatch "${SBATCH_FILE}"
