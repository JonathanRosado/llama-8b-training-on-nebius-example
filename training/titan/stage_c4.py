#!/usr/bin/env python3
"""Stage a bounded Parquet subset of `allenai/c4` to Nebius Object Storage.

Workflow:
1. Stream the English C4 split from Hugging Face.
2. Materialize bounded Parquet shard files locally.
3. Upload them with `rclone copy`, following the same tool choice already
   present in the vendored Soperator sync helpers.

The destination object store is the cold/source-of-truth tier. Path B training
reads the staged subset through the Slurm jail mount at `/mnt/datasets/c4`,
backed by the cluster-level Mountpoint-S3 CSI volume rather than HF streaming.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from datasets import Dataset, load_dataset


DEFAULT_DATASET = "allenai/c4"
DEFAULT_CONFIG = "en"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024 * 1024
DEFAULT_ROWS_PER_FILE = 25_000
DEFAULT_DESTINATION_URI = "s3://nebius-c4-datasets/allenai-c4/en/"
DEFAULT_RCLONE_ENDPOINT = "https://storage.eu-north2.nebius.cloud"


@dataclass
class StageResult:
    status: str
    dataset: str
    config_name: str
    estimated_bytes: int
    documents: int
    parquet_files: list[str]
    local_output_dir: str
    destination_uri: str
    rclone_target: str
    rclone_endpoint: str


def _estimate_row_bytes(sample: dict[str, object]) -> int:
    total = 0
    for key, value in sample.items():
        total += len(key.encode("utf-8"))
        if value is None:
            continue
        total += len(str(value).encode("utf-8"))
    return total


def _write_chunk(records: list[dict[str, object]], output_dir: Path, shard_index: int) -> str:
    shard_path = output_dir / f"c4-en-train-{shard_index:05d}.parquet"
    Dataset.from_list(records).to_parquet(str(shard_path))
    return str(shard_path)


def _run_rclone_copy(local_dir: Path, rclone_target: str, rclone_endpoint: str) -> None:
    subprocess.run(
        [
            "rclone",
            "copy",
            str(local_dir),
            rclone_target,
            "--s3-provider=AWS",
            f"--s3-endpoint={rclone_endpoint}",
            "--progress",
            "--links",
            "--use-mmap",
            "--bwlimit=1000M",
            "--transfers=64",
            "--buffer-size=512Mi",
            "--multi-thread-streams=24",
            "--multi-thread-chunk-size=128Mi",
            "--multi-thread-cutoff=4Gi",
            "--multi-thread-write-buffer-size=256Mi",
            "--checkers=16",
            "--use-server-modtime",
            "--fast-list",
            "--s3-no-head-object",
            "--s3-chunk-size=32M",
        ],
        check=True,
    )


def _human_summary(result: StageResult) -> str:
    return (
        f"status={result.status} docs={result.documents} "
        f"parquet_files={len(result.parquet_files)} bytes_est={result.estimated_bytes}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage a bounded Parquet subset of allenai/c4 to Nebius Object Storage."
    )
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help="Hugging Face dataset path.",
    )
    parser.add_argument(
        "--config-name",
        default=DEFAULT_CONFIG,
        help="Hugging Face dataset config name.",
    )
    parser.add_argument(
        "--destination-uri",
        default=DEFAULT_DESTINATION_URI,
        help="Human-readable destination URI for reports and manifests.",
    )
    parser.add_argument(
        "--rclone-target",
        required=True,
        help="rclone destination, for example :s3:nebius-c4-datasets/allenai-c4/en/.",
    )
    parser.add_argument(
        "--rclone-endpoint",
        default=DEFAULT_RCLONE_ENDPOINT,
        help="Nebius Object Storage S3 endpoint passed through to rclone.",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="Approximate upper bound for staged raw content bytes.",
    )
    parser.add_argument(
        "--rows-per-file",
        type=int,
        default=DEFAULT_ROWS_PER_FILE,
        help="Number of records per local Parquet shard before flush.",
    )
    parser.add_argument(
        "--local-output-dir",
        default="",
        help="Optional local directory. Defaults to a temporary directory.",
    )
    parser.add_argument(
        "--keep-local",
        action="store_true",
        help="Retain the local Parquet staging directory after upload.",
    )
    parser.add_argument(
        "--dump-json",
        action="store_true",
        help="Print only the final JSON payload.",
    )
    args = parser.parse_args()

    local_output_dir = (
        Path(args.local_output_dir)
        if args.local_output_dir
        else Path(tempfile.mkdtemp(prefix="stage-c4-"))
    )
    local_output_dir.mkdir(parents=True, exist_ok=True)

    dataset = load_dataset(
        args.dataset,
        name=args.config_name,
        split="train",
        streaming=True,
    )

    estimated_bytes = 0
    documents = 0
    shard_index = 0
    shard_records: list[dict[str, object]] = []
    parquet_files: list[str] = []

    for sample in dataset:
        sample_dict = dict(sample)
        shard_records.append(sample_dict)
        documents += 1
        estimated_bytes += _estimate_row_bytes(sample_dict)

        if len(shard_records) >= args.rows_per_file:
            parquet_files.append(_write_chunk(shard_records, local_output_dir, shard_index))
            shard_index += 1
            shard_records = []

        if estimated_bytes >= args.max_bytes:
            break

    if shard_records:
        parquet_files.append(_write_chunk(shard_records, local_output_dir, shard_index))

    manifest_path = local_output_dir / "manifest.json"
    manifest = {
        "dataset": args.dataset,
        "config_name": args.config_name,
        "estimated_bytes": estimated_bytes,
        "documents": documents,
        "destination_uri": args.destination_uri,
        "rclone_target": args.rclone_target,
        "parquet_files": [Path(path).name for path in parquet_files],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    _run_rclone_copy(local_output_dir, args.rclone_target, args.rclone_endpoint)

    result = StageResult(
        status="ok",
        dataset=args.dataset,
        config_name=args.config_name,
        estimated_bytes=estimated_bytes,
        documents=documents,
        parquet_files=[Path(path).name for path in parquet_files],
        local_output_dir=str(local_output_dir),
        destination_uri=args.destination_uri,
        rclone_target=args.rclone_target,
        rclone_endpoint=args.rclone_endpoint,
    )
    payload = json.dumps(asdict(result), indent=2, sort_keys=True)
    if args.dump_json:
        print(payload)
    else:
        print(_human_summary(result))
        print(payload)

    if not args.keep_local and not args.local_output_dir:
        shutil.rmtree(local_output_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
