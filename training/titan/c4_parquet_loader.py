#!/usr/bin/env python3
"""Register and inspect a Parquet-backed C4 dataset for TorchTitan.

This keeps the native TorchTitan `c4` loader untouched. The built-in loader
expects `load_dataset(dataset_path, name="en", split=..., streaming=True)`,
which matches Hugging Face Hub semantics and is not a drop-in fit for a raw
Parquet mount. This module adds a separate `c4_parquet` dataset entry that
loads local Parquet shards via `load_dataset("parquet", data_files=...)`.

Production note:
    The long-term mount path should be a K8s-level Mountpoint for Amazon S3
    integration (`docs.nebius.com/object-storage/interfaces/mountpoint-s3`).
    Path B mounts the staged Parquet shards into the Slurm jail at the
    canonical dataset path `/mnt/datasets/c4`.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any

from datasets import load_dataset


DEFAULT_DATASET_NAME = "c4_parquet"
DEFAULT_GLOB = "**/*.parquet"


@dataclass
class LoaderResult:
    status: str
    dataset_name: str
    dataset_path: str
    parquet_files: list[str]
    registered: bool
    sample_keys: list[str]


def _resolve_parquet_files(dataset_path: str, glob_pattern: str = DEFAULT_GLOB) -> list[str]:
    root = Path(dataset_path)
    files = sorted(str(path) for path in root.glob(glob_pattern) if path.is_file())
    if not files:
        raise FileNotFoundError(
            f"no Parquet files found under {dataset_path!r} with glob {glob_pattern!r}"
        )
    return files


def load_c4_parquet_dataset(dataset_path: str, split: str = "train"):
    data_files = _resolve_parquet_files(dataset_path)
    return load_dataset("parquet", data_files=data_files, split=split)


def process_c4_parquet_text(sample: dict[str, Any]) -> str:
    return sample["text"]


def register_c4_parquet_dataset(
    dataset_path: str,
    dataset_name: str = DEFAULT_DATASET_NAME,
) -> None:
    from torchtitan.hf_datasets import DatasetConfig
    from torchtitan.hf_datasets import text_datasets

    text_datasets.DATASETS[dataset_name] = DatasetConfig(
        path=dataset_path,
        loader=partial(load_c4_parquet_dataset, split="train"),
        sample_processor=process_c4_parquet_text,
    )


def _human_summary(result: LoaderResult) -> str:
    return (
        f"status={result.status} dataset={result.dataset_name} "
        f"files={len(result.parquet_files)} registered={result.registered}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register a local Parquet-backed C4 dataset for TorchTitan."
    )
    parser.add_argument(
        "--dataset-path",
        default="/mnt/datasets/c4",
        help="Root directory containing staged C4 Parquet files.",
    )
    parser.add_argument(
        "--dataset-name",
        default=DEFAULT_DATASET_NAME,
        help="TorchTitan dataset name to register.",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="Only inspect the Parquet path without mutating TorchTitan's dataset registry.",
    )
    parser.add_argument(
        "--dump-json",
        action="store_true",
        help="Print only the final JSON payload.",
    )
    args = parser.parse_args()

    parquet_files = _resolve_parquet_files(args.dataset_path)
    sample = load_dataset("parquet", data_files=parquet_files[:1], split="train[:1]")[0]

    registered = False
    if not args.no_register:
        register_c4_parquet_dataset(args.dataset_path, args.dataset_name)
        registered = True

    result = LoaderResult(
        status="ok",
        dataset_name=args.dataset_name,
        dataset_path=args.dataset_path,
        parquet_files=parquet_files[:8],
        registered=registered,
        sample_keys=sorted(sample.keys()),
    )
    payload = json.dumps(asdict(result), indent=2, sort_keys=True)
    if args.dump_json:
        print(payload)
    else:
        print(_human_summary(result))
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
