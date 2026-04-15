#!/usr/bin/env python3
"""Minimal FSDP2 demo training run for the Path A Nebius PoC.

Purpose:
    Prove end-to-end that the Slurm+Slinky cluster built under
    terraform/path-a/ can run a canonical distributed-training workload
    using:
      * torchrun for rank bootstrap
      * PyTorch 2.x FSDP2 for fully-sharded data parallelism
      * NCCL over InfiniBand for gradient all-reduce
      * Shared NFS for checkpoint durability across ranks

    The model is intentionally tiny (~125M) so the demo fits in a short
    Slurm step and the validator exit criteria (loss decreasing over
    ≥20 steps, checkpoint written, job exit 0) are hit within minutes,
    not hours. The same pipeline shape scales to 100B+ parameter runs
    on the customer's 500+ GPU production cluster by swapping the
    model factory and batch size.

Run via: training/sbatch/fsdp_demo.sbatch (torchrun launches one
instance of this script per GPU across both nodes).
"""
import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

import torch
import torch.distributed as dist
import torch.nn as nn
# WHY (FSDP v1 API): `fully_shard` (FSDP2) is not present in every
# pytorch NVIDIA-container release — nvcr.io/nvidia/pytorch:24.12-py3
# ships torch 2.6.0a with the old FSDP v1 API only. FullyShardedDataParallel
# is stable since torch 1.12 and works the same way across every
# PyTorch we'd hand a customer for a 500+ GPU run.
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy
import functools


def log(msg):
    rank = int(os.environ.get("RANK", "0"))
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] rank={rank} {msg}", flush=True)


class TinyTransformer(nn.Module):
    """~125M param transformer — small enough to train quickly, large
    enough to exercise FSDP sharding and cross-GPU NCCL traffic."""

    def __init__(self, vocab_size=32000, hidden=768, n_layers=12, n_heads=12):
        super().__init__()
        self.embed = nn.Embedding(vocab_size, hidden)
        # Standard TransformerEncoder; FSDP will shard each layer's
        # parameters across ranks.
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=hidden,
            nhead=n_heads,
            dim_feedforward=hidden * 4,
            batch_first=True,
            activation="gelu",
        )
        self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
        self.head = nn.Linear(hidden, vocab_size)

    def forward(self, tokens):
        x = self.embed(tokens)
        x = self.encoder(x)
        return self.head(x)


def main(args):
    rank = int(os.environ["RANK"])
    world = int(os.environ["WORLD_SIZE"])
    local_rank = int(os.environ["LOCAL_RANK"])
    device = torch.device(f"cuda:{local_rank}")
    torch.cuda.set_device(device)

    log(f"init world={world} local_rank={local_rank} device={device}")
    dist.init_process_group(backend="nccl", device_id=device)

    model = TinyTransformer().to(device)
    # WHY (auto_wrap_policy on TransformerEncoderLayer): FSDP v1 groups
    # parameters into "units" that are all-gathered together. Wrapping
    # each encoder layer as its own unit gives per-layer all-gather/scatter,
    # matching FSDP2's fully_shard semantics. Without a wrap policy FSDP
    # wraps the root only, which loses the memory benefit.
    auto_wrap_policy = functools.partial(
        transformer_auto_wrap_policy,
        transformer_layer_cls={nn.TransformerEncoderLayer},
    )
    fsdp_model = FSDP(model, auto_wrap_policy=auto_wrap_policy, device_id=device)

    optimizer = torch.optim.AdamW(fsdp_model.parameters(), lr=args.lr, weight_decay=0.1)
    loss_fn = nn.CrossEntropyLoss()

    losses = []
    torch.manual_seed(args.seed + rank)
    batch_size = args.batch_size
    seq_len = args.seq_len
    vocab = 32000

    log(f"training {args.steps} steps, bsz={batch_size}, seq={seq_len}")
    fsdp_model.train()
    step_start = time.perf_counter()

    for step in range(args.steps):
        # Synthetic causal-LM style batch: random tokens, target = shift-1.
        tokens = torch.randint(0, vocab, (batch_size, seq_len), device=device)
        targets = torch.roll(tokens, shifts=-1, dims=-1)

        optimizer.zero_grad(set_to_none=True)
        logits = fsdp_model(tokens)
        loss = loss_fn(logits.reshape(-1, vocab), targets.reshape(-1))
        loss.backward()
        optimizer.step()

        # All-reduce the loss across ranks for reporting.
        loss_val = loss.detach().clone()
        dist.all_reduce(loss_val, op=dist.ReduceOp.AVG)
        losses.append(loss_val.item())

        if rank == 0:
            elapsed = time.perf_counter() - step_start
            log(f"step {step:3d}/{args.steps} loss={loss_val.item():.4f} elapsed={elapsed:.1f}s")

    total_elapsed = time.perf_counter() - step_start

    # Sharded checkpoint — each rank saves its own shard to
    # rank-named files. This is the production-canonical pattern for
    # 500+ GPU clusters (full gathers don't scale). For smaller demos
    # we could gather to rank 0, but that path hit a hang with FSDP v1's
    # StateDictType.FULL_STATE_DICT on this container. Sharded is faster
    # and matches `torch.distributed.checkpoint`'s default writer shape.
    ckpt_dir = Path(args.checkpoint_dir)
    if rank == 0:
        ckpt_dir.mkdir(parents=True, exist_ok=True)
    dist.barrier()

    shard = fsdp_model.state_dict()  # each rank's local shard
    shard_file = ckpt_dir / f"shard_rank{rank:02d}.pt"
    torch.save(shard, shard_file)
    log(f"wrote shard {shard_file.name} ({sum(p.numel() for p in shard.values()):d} params)")

    dist.barrier()

    if rank == 0:
        # Compute whether loss decreased over the last 10 steps using a
        # tolerance band rather than strict monotonicity — stochastic
        # gradient noise makes strict step-by-step decrease unrealistic
        # even in healthy training. The meaningful signal is that the
        # net trend is downward.
        last10 = losses[-10:] if len(losses) >= 10 else losses
        net_last10_decrease = last10[0] - last10[-1] if last10 else 0.0
        overall_decrease = losses[0] - losses[-1] if losses else 0.0

        metrics = {
            "steps": args.steps,
            "world_size": world,
            "batch_size": batch_size,
            "seq_len": seq_len,
            "losses": losses,
            "first_loss": losses[0] if losses else None,
            "last_loss": losses[-1] if losses else None,
            "last10_losses": last10,
            "net_last10_decrease": round(net_last10_decrease, 4),
            "overall_decrease": round(overall_decrease, 4),
            "training_decreased": overall_decrease > 0 and net_last10_decrease > 0,
            "total_elapsed_seconds": round(total_elapsed, 2),
            "tokens_per_second_per_rank": round(
                (batch_size * seq_len * args.steps) / total_elapsed, 1
            ),
            "gpu_name": torch.cuda.get_device_name(0),
            "num_shards": world,
        }
        with open(ckpt_dir / "metrics.json", "w") as f:
            json.dump(metrics, f, indent=2)
        log(f"wrote {ckpt_dir}/metrics.json + {world} sharded model files")
        log(f"first_loss={metrics['first_loss']:.4f} last_loss={metrics['last_loss']:.4f}")
        log(f"training_decreased={metrics['training_decreased']}")

    dist.barrier()
    dist.destroy_process_group()
    log("done")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--steps", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--seq-len", type=int, default=512)
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--checkpoint-dir",
        type=str,
        default=os.environ.get(
            "CHECKPOINT_DIR",
            f"/nfs/shared/checkpoints/fsdp_demo/{os.environ.get('SLURM_JOB_ID', 'manual')}",
        ),
    )
    args = p.parse_args()
    sys.exit(main(args) or 0)
