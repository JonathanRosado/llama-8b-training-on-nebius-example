#!/usr/bin/env python3
"""Thin MLflow wrapper for TorchTitan training.

This wrapper preserves the existing TorchTitan code path and runtime shape:
it starts an MLflow run, logs high-level run parameters, then monkeypatches
TorchTitan's TensorBoard logger so the same per-step metrics flowing to
TensorBoard are also forwarded to MLflow.

Re-run note:
    Once the Path B MLflow marketplace release is deployed and reachable,
    re-run `fsdp_full` through this wrapper to verify the end-to-end path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any


_MLFLOW_KEY_RE = re.compile(r"[^A-Za-z0-9_\-. /]")


def _sanitize_metric_key(key: str) -> str:
    """MLflow only allows [alnum _ - . space /]. TorchTitan emits keys like
    `throughput(tps)` that MLflow rejects with INVALID_PARAMETER_VALUE. We
    replace any disallowed char with `_` and collapse runs."""
    cleaned = _MLFLOW_KEY_RE.sub("_", key)
    return re.sub(r"_+", "_", cleaned).strip("_")


@dataclass
class WrapperResult:
    status: str
    tracking_uri: str | None
    experiment_name: str
    run_name: str | None
    run_id: str | None
    rank: int
    forwarded_metric_keys: list[str]
    torchtitan_argv: list[str]
    duration_s: float


def _parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Wrap torchtitan.train with MLflow run setup and metric forwarding."
    )
    parser.add_argument(
        "--experiment-name",
        default=os.environ.get("MLFLOW_EXPERIMENT_NAME", "torchtitan"),
        help="MLflow experiment name.",
    )
    parser.add_argument(
        "--run-name",
        default=os.environ.get("MLFLOW_RUN_NAME"),
        help="Optional MLflow run name. Defaults to the config name when present.",
    )
    parser.add_argument(
        "--tracking-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI"),
        help="MLflow tracking URI. Falls back to the environment variable.",
    )
    parser.add_argument(
        "--dump-json",
        action="store_true",
        help="Print only the final JSON status payload.",
    )
    # argparse.REMAINDER is buggy on Python 3.12+ (doesn't consume args starting
    # with `--`). Use parse_known_args() and treat unknown args as torchtitan
    # passthrough. Wrapper args must appear BEFORE torchtitan args on the CLI.
    args, forwarded = parser.parse_known_args()
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    if not forwarded:
        parser.error("no TorchTitan arguments provided; pass them after wrapper flags")
    return args, forwarded


def _env_rank() -> int:
    for key in ("RANK", "SLURM_PROCID", "LOCAL_RANK"):
        value = os.environ.get(key)
        if value is not None:
            return int(value)
    return 0


def _extract_params(argv: list[str]) -> dict[str, str]:
    params: dict[str, str] = {"command": "python -m torchtitan.train"}
    i = 0
    while i < len(argv):
        token = argv[i]
        if not token.startswith("--"):
            i += 1
            continue
        key = token[2:]
        value = "true"
        if "=" in key:
            key, value = key.split("=", 1)
        elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            value = argv[i + 1]
            i += 1
        params[key] = value
        i += 1
    return params


def _default_run_name(explicit_name: str | None, params: dict[str, str]) -> str | None:
    if explicit_name:
        return explicit_name
    config_name = params.get("config")
    module_name = params.get("module")
    if config_name and module_name:
        return f"{module_name}:{config_name}"
    return config_name or module_name


def _human_summary(result: WrapperResult) -> str:
    metric_keys = ", ".join(result.forwarded_metric_keys) or "none"
    return (
        f"status={result.status} rank={result.rank} experiment={result.experiment_name} "
        f"run_id={result.run_id or 'none'} metrics={metric_keys} duration_s={result.duration_s:.2f}"
    )


def main() -> int:
    args, torchtitan_args = _parse_args()
    rank = _env_rank()
    start = time.time()
    params = _extract_params(torchtitan_args)
    run_name = _default_run_name(args.run_name, params)
    forwarded_metric_keys = [
        "loss_metrics/global_avg_loss",
        "loss_metrics/global_max_loss",
        "grad_norm",
        "throughput(tps)",
        "tflops",
        "mfu(%)",
        "memory/max_reserved(GiB)",
        "memory/max_reserved(%)",
    ]

    if args.tracking_uri:
        os.environ["MLFLOW_TRACKING_URI"] = args.tracking_uri

    sys.argv = ["torchtitan.train", *torchtitan_args]

    mlflow_run = None
    active = rank == 0

    if active:
        try:
            import mlflow
        except ModuleNotFoundError as exc:
            raise SystemExit(
                "mlflow is not installed in the active Python environment; "
                "install it in the jail venv before using this wrapper."
            ) from exc

        mlflow.set_experiment(args.experiment_name)
        tags = {
            "launcher": "training/titan/mlflow_wrapper.py",
            "framework": "torchtitan",
            "scheduler": "slurm" if os.environ.get("SLURM_JOB_ID") else "local",
        }
        if os.environ.get("SLURM_JOB_ID"):
            tags["slurm.job_id"] = os.environ["SLURM_JOB_ID"]
        mlflow_run = mlflow.start_run(run_name=run_name)
        mlflow.set_tags(tags)
        mlflow.log_params(params)

        from torchtitan.components import metrics as titan_metrics

        original_log = titan_metrics.TensorBoardLogger.log

        def patched_log(self: Any, metrics: dict[str, Any], step: int) -> None:
            original_log(self, metrics, step)
            filtered = {
                _sanitize_metric_key(key): value
                for key, value in metrics.items()
                if isinstance(value, (int, float))
            }
            if filtered:
                try:
                    mlflow.log_metrics(filtered, step=step)
                except Exception as exc:
                    # Never let MLflow hiccups kill the training run.
                    print(f"[mlflow_wrapper] warning: log_metrics failed: {exc}", file=sys.stderr)

        titan_metrics.TensorBoardLogger.log = patched_log

    run_id = None
    exit_code = 1  # default — any unhandled exception leaves this as 1
    try:
        runpy.run_module("torchtitan.train", run_name="__main__")
    except SystemExit as exc:
        if exc.code is None:
            exit_code = 0
        elif isinstance(exc.code, int):
            exit_code = exc.code
        else:
            exit_code = 1
    except BaseException:
        exit_code = 1
        raise
    else:
        exit_code = 0
    finally:
        if mlflow_run is not None:
            import mlflow

            run_id = mlflow_run.info.run_id
            if exit_code == 0:
                mlflow.set_tag("run_status", "completed")
            else:
                mlflow.set_tag("run_status", f"failed:{exit_code}")
            mlflow.end_run(status="FINISHED" if exit_code == 0 else "FAILED")

    result = WrapperResult(
        status="ok" if exit_code == 0 else "error",
        tracking_uri=args.tracking_uri or os.environ.get("MLFLOW_TRACKING_URI"),
        experiment_name=args.experiment_name,
        run_name=run_name,
        run_id=run_id,
        rank=rank,
        forwarded_metric_keys=forwarded_metric_keys,
        torchtitan_argv=torchtitan_args,
        duration_s=time.time() - start,
    )
    payload = json.dumps(asdict(result), indent=2, sort_keys=True)
    if args.dump_json:
        print(payload)
    else:
        print(_human_summary(result))
        print(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
