#!/usr/bin/env python3
"""Multi-node NCCL allreduce bandwidth benchmark — torchrun launched.

Purpose:
    The multi-node NCCL check in validator/validate.py used to launch
    `all_reduce_perf_mpi` via `srun --mpi=pmix`, which pulls in the
    hpc-benchmarks container's HPC-X MPI + UCX stack. On Path A's
    Ethernet-only cluster that stack fails to initialize multi-node
    (UCX looks for IB HCAs and can't find a fabric).

    This script replaces that benchmark with the production-canonical
    pattern for 500+ GPU Ethernet customers:
      * torchrun handles rank discovery and GPU binding
      * torch.distributed(backend="nccl") drives NCCL directly
      * NCCL uses sockets (per env vars set in the sbatch/_srun_container)
      * We measure bus bandwidth at several message sizes using the
        same formula nccl-tests uses: busbw = size * 2 * (N-1) / N / time

    The last line printed on rank 0 is a single-line JSON:
        NCCL_BENCH_RESULT={"world_size": N, "sizes": [...], "avg_busbw_gbps": X}
    The validator parses that line to compute its verdict.

Launch via:
    torchrun --nnodes=N --nproc-per-node=8 \
        --rdzv-id=$SLURM_JOB_ID --rdzv-backend=c10d \
        --rdzv-endpoint=$MASTER_ADDR:29500 \
        /nfs/home/training/nccl_bench.py --min-bytes 536870912 --max-bytes 8589934592 --factor 2
"""
import argparse
import datetime
import json
import os
import sys
import time

import torch
import torch.distributed as dist


def log(msg):
    rank = int(os.environ.get("RANK", "0"))
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] rank={rank} {msg}", flush=True)


def main(args):
    rank = int(os.environ["RANK"])
    world = int(os.environ["WORLD_SIZE"])
    local_rank = int(os.environ["LOCAL_RANK"])
    device = torch.device(f"cuda:{local_rank}")
    torch.cuda.set_device(device)

    dist.init_process_group(backend="nccl", device_id=device)
    if rank == 0:
        log(f"initialized world={world} backend=nccl")

    # Build the size sweep: powers of `factor` from min_bytes up to max_bytes.
    sizes = []
    s = args.min_bytes
    while s <= args.max_bytes:
        sizes.append(s)
        s *= args.factor

    results = []
    for size_bytes in sizes:
        elem = size_bytes // 4  # float32
        tensor = torch.ones(elem, dtype=torch.float32, device=device)

        # Warmup.
        for _ in range(args.warmup):
            dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
        torch.cuda.synchronize()

        # Timed.
        dist.barrier()
        start = time.perf_counter()
        for _ in range(args.iters):
            dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
        torch.cuda.synchronize()
        elapsed = (time.perf_counter() - start) / args.iters

        # nccl-tests busbw formula for allreduce (ring):
        #   algbw = size / time (GB/s)
        #   busbw = algbw * 2 * (N-1) / N
        algbw_gbps = (size_bytes / elapsed) / 1e9
        busbw_gbps = algbw_gbps * 2.0 * (world - 1) / world

        if rank == 0:
            log(f"size={size_bytes:>12d} B  time={elapsed*1e6:>8.1f} us  "
                f"algbw={algbw_gbps:>6.2f} GB/s  busbw={busbw_gbps:>6.2f} GB/s")
        results.append({
            "bytes": size_bytes,
            "time_us": round(elapsed * 1e6, 2),
            "algbw_gbps": round(algbw_gbps, 3),
            "busbw_gbps": round(busbw_gbps, 3),
        })

    if rank == 0:
        avg_busbw = sum(r["busbw_gbps"] for r in results) / len(results)
        # Single-line JSON for the validator to parse.
        result = {
            "world_size": world,
            "iters": args.iters,
            "warmup": args.warmup,
            "sizes": results,
            "avg_busbw_gbps": round(avg_busbw, 3),
        }
        print(f"NCCL_BENCH_RESULT={json.dumps(result, separators=(',', ':'))}", flush=True)

    dist.barrier()
    dist.destroy_process_group()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--min-bytes", type=int, default=512 * 1024 * 1024)
    p.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024 * 1024)
    p.add_argument("--factor", type=int, default=2)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--iters", type=int, default=10)
    args = p.parse_args()
    sys.exit(main(args) or 0)
