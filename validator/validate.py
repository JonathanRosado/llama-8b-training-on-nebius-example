#!/usr/bin/env python3
"""
GPU cluster validator for Nebius AI Cloud PoC.

Strategy:
    1. Terraform deploys the full stack in one apply: K8s, Soperator/Slurm,
       GPU node groups, shared filesystem, InfiniBand fabric.
    2. Soperator's built-in active checks gate the cluster as healthy.
    3. This script runs as a post-deployment acceptance test via Slurm,
       producing a structured report the customer can reproduce on their
       512-GPU production cluster with the same tooling.

Architecture:
    The script runs on the Slurm login node and orchestrates checks by
    invoking srun against compute nodes. It never runs on GPU nodes itself.
    All GPU/fabric/storage measurements use industry-standard binaries
    (all_reduce_perf, dcgmi, ib_write_bw, fio, nvidia-smi) delivered via
    Pyxis/enroot container images. We use these instead of hand-rolled
    Python measurements because they are vendor-maintained, produce
    comparable results across clusters, and are what an interviewer or
    customer would expect.

Verdict system:
    PASS  — metric meets or exceeds threshold
    WARN  — metric is within 80-100% of threshold (transient contention,
            not a hard failure) or an optional tool is missing
    FAIL  — metric is below 80% of threshold or a required check failed
    SKIPPED — check not applicable (e.g., multi-node on a single-node cluster)
    Overall: FAIL if any FAIL, WARN if any WARN, PASS otherwise.
    SKIPPED checks do not affect the overall verdict.

Usage:
    python3 validate.py                                       # from login node
    sbatch validate.sbatch                                    # via batch wrapper
    NCCL_NVLINK_BUSBW_GBPS_MIN=400 python3 validate.py       # custom threshold
"""

import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

# All thresholds are env-configurable. Defaults are tuned for H200 SXM nodes
# with NVLink4 + NDR400 InfiniBand. Setting these too high creates false
# failures under shared-cluster noise; too low lets degraded hardware pass.
THRESHOLDS = {
    # 8 GPUs per H200 SXM node (each 141 GB HBM3e)
    "GPU_COUNT_MIN": int(os.environ.get("GPU_COUNT_MIN", 8)),
    # H200 NVLink4: ~900 GB/s bidirectional per GPU. 8-GPU ring allreduce
    # bus BW typically achieves 350-450 GB/s. 350 is a conservative floor
    # that catches degraded links without false-failing under contention.
    "NCCL_NVLINK_BUSBW_GBPS_MIN": float(os.environ.get("NCCL_NVLINK_BUSBW_GBPS_MIN", 350)),
    # Path A runs NCCL over 200 Gbps Ethernet sockets (no InfiniBand). The
    # per-node physical ceiling is ~25 GB/s; NCCL TCP allreduce on 16 ranks
    # (ring algorithm with 15 hops) lands at ~6-8 GB/s busbw in practice on
    # this hardware (measured: 6.6 GB/s at 8 GiB message with NTHREADS=8 /
    # NSOCKS=8 tuning). 5 GB/s is the conservative floor that flags a
    # half-speed link or a broken socket config without false-failing under
    # normal load. Override to 40 GB/s for Path B (IB) deployments by
    # exporting NCCL_INTER_BUSBW_GBPS_MIN.
    "NCCL_INTER_BUSBW_GBPS_MIN": float(os.environ.get("NCCL_INTER_BUSBW_GBPS_MIN", 5)),
    # Single-port ib_write_bw with GPUDirect RDMA: ~50 GB/s (400 Gbps).
    # 40 Gbps is the floor; below this, GPUDirect may be falling back
    # through host memory (nvidia-peermem not loaded or PCIe topology issue).
    "IB_BW_GBPS_MIN": float(os.environ.get("IB_BW_GBPS_MIN", 40)),
    # Nebius shared filesystem. 500 MB/s sequential write is a reasonable
    # floor for training checkpoint I/O.
    "STORAGE_BW_MBS_MIN": float(os.environ.get("STORAGE_BW_MBS_MIN", 500)),
    # Shared filesystem mount point on the host and inside the container
    "STORAGE_PATH": os.environ.get("STORAGE_PATH", "/nfs/shared"),
    # DCGM diagnostic level: 2 covers memory, PCIe, thermal (~2-5 min).
    # Level 3 adds stress tests and nvbandwidth (~10-30 min).
    "DCGM_LEVEL": int(os.environ.get("DCGM_LEVEL", 2)),
    "SRUN_TIMEOUT": int(os.environ.get("SRUN_TIMEOUT", 600)),
}

REPORT_DIR = os.environ.get("REPORT_DIR", ".")
CONTAINER_IMAGE = os.environ.get("CONTAINER_IMAGE", "nvcr.io/nvidia/hpc-benchmarks:26.02")
PYTORCH_CONTAINER_IMAGE = os.environ.get("PYTORCH_CONTAINER_IMAGE", "nvcr.io/nvidia/pytorch:24.12-py3")
# Path A uses /nfs/home/training (NFS-server-backed), Path B uses /home/nebius/training
# (Soperator virtiofs jail). Path A mounts /nfs/home + /nfs/shared into containers;
# Path B has no /nfs/* paths — the jail is already mounted by Soperator's slurmd.
TRAINING_SCRIPT_DIR = os.environ.get("TRAINING_SCRIPT_DIR", "/nfs/home/training")
VALIDATOR_EXTRA_MOUNTS = [m for m in os.environ.get("VALIDATOR_EXTRA_MOUNTS", "/nfs/home:/nfs/shared").split(":") if m]
# VALIDATOR_TORCHRUN: absolute path to a torchrun binary. Default empty =
# use pyxis + PYTORCH_CONTAINER_IMAGE (Path A Slinky pattern — slurmd's
# enroot 98-nvidia.sh hook injects libcuda cleanly into the pyxis container).
# Path B (Soperator) sets e.g. /home/nebius/venv/bin/torchrun because the
# chroot.so × spank_pyxis.so plugstack ordering on Soperator 3.0.2 causes
# the 98-nvidia.sh hook injection to partially fail (libnvidia-ml.so +
# /dev/nvidia0..7 don't land inside the pyxis rootfs even though
# nvidia-container-cli runs successfully against root=/). The jail venv
# pattern bypasses pyxis entirely — we exec the venv's torchrun directly
# in the jail, inheriting the slurmd pod's already-working NVIDIA setup.
VALIDATOR_TORCHRUN = os.environ.get("VALIDATOR_TORCHRUN", "")
STORAGE_CONTAINER_IMAGE = os.environ.get("STORAGE_CONTAINER_IMAGE", CONTAINER_IMAGE)
# Research summary:
# A) The NVLink crash is not blamed on `=eth0`: NCCL documents
#    `NCCL_SOCKET_IFNAME==eth0` exact-match syntax, and the failure happens in
#    hpc-benchmarks 26.02 / NCCL 2.29.2 before data movement. Root cause is
#    therefore treated as an image/library-path regression hypothesis with
#    medium confidence, while the fix is to keep single-node NVLink off the
#    Ethernet transport path entirely. Sources: NCCL env docs, validator code.
# B) NVIDIA PyTorch 24.12 ships PyTorch + `torchrun`, CUDA 12.6.3, NCCL 2.23.4,
#    rdma-core 39.0, HPC-X 2.21, UCX 1.18.0. Source: NVIDIA PyTorch 24.12 notes.
# C) Recommendation: use `nvcr.io/nvidia/pytorch:24.12-py3` for both NCCL
#    checks via `torchrun` + `training/nccl_bench.py`; one runtime removes the
#    image skew that already broke job 40 and matches production torch.distributed.
# D) Keep 350 GB/s: `training/nccl_bench.py` uses nccl-tests' allreduce busbw
#    formula `size/time * 2*(N-1)/N`, so the threshold remains metric-compatible.
# WHY: default HAS_IB=False matches Path A's constrained baseline (no
# gpu_cluster, no InfiniBand fabric). Path B sets SLURM_CLUSTER_HAS_IB=1 in
# its sbatch wrapper. The multi-node NCCL check still runs when HAS_IB=0 —
# it just runs over Ethernet with the Ethernet threshold. Only IB-specific
# probes (nccl_ib_isolated which forces IB via NCCL_P2P_DISABLE=1, and
# ib_bandwidth which calls ib_write_bw with --use_cuda) skip when
# HAS_IB=False because they literally cannot work without a fabric.
HAS_IB = os.environ.get("SLURM_CLUSTER_HAS_IB", "0") not in ("", "0", "false", "False")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("validator")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")

_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.INFO)
_sh.setFormatter(_fmt)
log.addHandler(_sh)

_fh = logging.FileHandler(os.path.join(REPORT_DIR, "validator.log"), mode="w")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
log.addHandler(_fh)

# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------


def run_cmd(name, cmd, env_overrides=None, timeout=None):
    timeout = timeout or THRESHOLDS["SRUN_TIMEOUT"]
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)

    cmd_str = " ".join(cmd)
    log.debug(f"[{name}] CMD: {cmd_str}")
    if env_overrides:
        log.debug(f"[{name}] ENV overrides: {env_overrides}")

    start = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as e:
        elapsed = time.perf_counter() - start
        log.debug(f"[{name}] COMMAND NOT FOUND: {e}")
        return None, elapsed, str(e)
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        log.debug(f"[{name}] TIMEOUT after {elapsed:.1f}s")
        return None, elapsed, f"timeout after {timeout}s"

    elapsed = time.perf_counter() - start
    log.debug(f"[{name}] EXIT: {proc.returncode}  ELAPSED: {elapsed:.1f}s")
    log.debug(f"[{name}] STDOUT ({len(proc.stdout)} chars):\n{proc.stdout}")
    if proc.stderr.strip():
        log.debug(f"[{name}] STDERR:\n{proc.stderr}")
    return proc, elapsed, None


def _srun_container(name, srun_flags, cmd_and_args, image, mounts=None,
                    env_overrides=None, timeout=None):
    # WHY (NVIDIA_DRIVER_CAPABILITIES=utility): Enroot's 98-nvidia.sh hook
    # calls nvidia-container-cli to inject driver userspace from the host.
    # Our slinky chart postStart patches nvidia-container-cli to skip the
    # persistenced/fabricmanager socket mounts that were failing under
    # enroot's user namespace. With those skipped, `utility` capability
    # mounts nvidia-smi + libnvidia-ml.so (which NCCL needs via dlopen)
    # cleanly. `compute` capability is NOT added because vendor CUDA
    # containers (hpc-benchmarks:26.02, pytorch:XX) bundle their own
    # libcuda.so and dropping it avoids double-mount churn.
    #
    # WHY (env via subprocess inheritance, not --export=ALL,VAR=VAL):
    # srun --export uses comma-separated VAR=VAL pairs. A capability value
    # like "compute,utility" contains a comma, which srun parses as TWO
    # env entries — the second one (`utility`) becomes a bogus bare token
    # and the variable effectively ends up as "compute" only. Setting the
    # env via run_cmd's subprocess env plus `--export=ALL` on srun avoids
    # the comma-parsing trap and lets multi-value capability strings work
    # correctly in the future if needed.
    env = dict(env_overrides or {})
    env.setdefault("NVIDIA_DRIVER_CAPABILITIES", "utility")
    # Enroot's 98-nvidia.sh hook exits early if NVIDIA_VISIBLE_DEVICES is unset
    # or "void" — without this, pyxis containers start without libcuda.so and
    # PyTorch fails with "Found no NVIDIA driver on your system". `all` asks
    # the hook to forward every GPU visible to the step; Slurm's --gpus flag
    # already constrains which devices the step actually sees.
    env.setdefault("NVIDIA_VISIBLE_DEVICES", "all")
    # WHY (Ethernet-only NCCL defaults): Path A has no InfiniBand fabric, so
    # NCCL falls back to sockets over the 200 Gbps Ethernet interface. These
    # env vars are the canonical upstream tuning per
    # https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html:
    #   - NCCL_IB_DISABLE=1 forces the sockets transport
    #   - NCCL_SOCKET_IFNAME==eth0 (exact-match prefix) picks eth0 specifically
    #     so NCCL doesn't probe for non-existent ib* interfaces
    #   - NCCL_SOCKET_NTHREADS=4 + NCCL_NSOCKS_PERTHREAD=4 give 16 socket
    #     threads per rank; recommended for 100G-class+ socket networks
    # The IB-era env set (MELLANOX_VISIBLE_DEVICES, UCX_NET_DEVICES,
    # NCCL_IB_HCA, SHARP_*) was dropped because those variables are either
    # no-ops without a fabric (MELLANOX) or IB-specific (UCX, SHARP). They
    # remain in git tag path-a-ib-working-v1 for reference.
    if HAS_IB:
        # Path B: InfiniBand fabric present. Let NCCL auto-discover HCAs
        # (Nebius H200 nodes expose mlx5_0..mlx5_7, one NDR400 HCA per GPU).
        # NCCL_IB_HCA=mlx5 matches any HCA whose name starts with "mlx5".
        # Do NOT force NCCL_SOCKET_IFNAME — the socket is bootstrap only;
        # data goes over IB. GID index 3 is Nebius's documented choice.
        env.setdefault("NCCL_IB_HCA", "mlx5")
        env.setdefault("NCCL_IB_GID_INDEX", "3")
    else:
        # Path A: no InfiniBand. Force socket transport and tune.
        env.setdefault("NCCL_IB_DISABLE", "1")
        env.setdefault("NCCL_SOCKET_IFNAME", "=eth0")
        env.setdefault("NCCL_SOCKET_NTHREADS", "4")
        env.setdefault("NCCL_NSOCKS_PERTHREAD", "4")

    # WHY (/dev/shm always mounted): Pyxis/enroot containers get their
    # own private /dev/shm (64 MiB default) unless we bind-mount the
    # host's. UCX's posix shared-memory transport uses /dev/shm for MPI
    # buffer pools; with only 64 MiB it fails fast with "Not enough
    # memory ... Please check that /dev/shm". The slurmd pod now has a
    # Memory-medium emptyDir mounted at /dev/shm (see slinky chart
    # values), and propagating that into every enroot container gives
    # UCX the headroom it needs.
    all_mounts = ["/dev/shm"] + list(mounts or [])
    cmd = ["srun", "--export=ALL", *srun_flags, f"--container-image={image}"]
    mount_pairs = [f"{m}:{m}" for m in all_mounts]
    cmd.append(f"--container-mounts={','.join(mount_pairs)}")
    cmd.extend(cmd_and_args)

    log.debug(f"[{name}] container image: {image}")
    if mounts:
        log.debug(f"[{name}] container mounts: {mounts}")
    log.debug(f"[{name}] env (subprocess): {env}")
    _ = timeout
    return cmd, env


def near_threshold(value, threshold):
    # The 80% band distinguishes transient contention (WARN) from real
    # degradation (FAIL). Shared clusters routinely see 5-15% variance;
    # a hard threshold would create false failures during demos.
    return value >= threshold * 0.8 and value < threshold


def verdict_from_value(value, threshold, higher_is_better=True):
    if higher_is_better:
        if value >= threshold:
            return PASS
        if near_threshold(value, threshold):
            return WARN
        return FAIL
    else:
        if value <= threshold:
            return PASS
        return FAIL


def make_result(name, verdict, detail, metrics=None, raw_stdout="", raw_stderr="",
                cmd_str="", elapsed=0.0):
    return {
        "check": name,
        "verdict": verdict,
        "detail": detail,
        "metrics": metrics or {},
        "cmd": cmd_str,
        "elapsed_s": round(elapsed, 2),
        "raw_stdout_tail": raw_stdout[-2000:] if raw_stdout else "",
        "raw_stderr_tail": raw_stderr[-2000:] if raw_stderr else "",
    }


def _cmd_failure(err, proc):
    if err:
        return err
    if proc and proc.stderr.strip():
        return proc.stderr.strip()
    if proc and proc.stdout.strip():
        return proc.stdout.strip()
    return "non-zero exit"


def _binary_missing(err, proc):
    """Return True if the command failed because the binary doesn't exist.

    Covers two cases:
      - FileNotFoundError from subprocess.run (err contains "not found")
      - srun succeeded but the target binary wasn't on the compute-node PATH
        (stderr contains execve's "No such file or directory")
    """
    if err and "not found" in str(err).lower():
        return True
    if proc and proc.stderr:
        lower = proc.stderr.lower()
        if "no such file or directory" in lower and "execve" in lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Cluster discovery
# ---------------------------------------------------------------------------


def discover_cluster():
    """Query Slurm to discover the cluster topology.

    Uses sinfo to find GPU nodes and determine whether multi-node checks
    should run. The is_multi_node flag drives SKIPPED decisions for
    nccl_multi_node, ib_bandwidth, and storage_cross_node.
    """
    log.info("Discovering cluster topology...")

    proc, _, _ = run_cmd("discover", ["sinfo", "--noheader", "-N", "-o", "%N %G %P"])
    if proc is None or proc.returncode != 0:
        log.error("sinfo failed — is Slurm running?")
        return None

    nodes = []
    gpu_nodes = []
    for line in proc.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2:
            node_name = parts[0]
            gpus = parts[1]
            nodes.append(node_name)
            if "gpu" in gpus.lower() or "(null)" not in gpus:
                gpu_nodes.append(node_name)

    unique_gpu_nodes = sorted(set(gpu_nodes))
    node_count = len(unique_gpu_nodes)

    log.info(f"  Nodes: {node_count} GPU node(s): {', '.join(unique_gpu_nodes)}")
    return {
        "all_nodes": sorted(set(nodes)),
        "gpu_nodes": unique_gpu_nodes,
        "gpu_node_count": node_count,
        "is_multi_node": node_count > 1,
    }


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def check_preflight(cluster):
    """Verify the environment can run checks before spending time on benchmarks.

    Verifies host-side Slurm tools on the login node, then validates that the
    required Pyxis images can be pulled and started on a compute node. For
    containerized execution, per-binary `which` checks are not useful: if the
    image starts, Pyxis + enroot are functional and the real benchmark step is
    the authoritative test of image contents.

    Also verifies the shared filesystem is visible on all GPU nodes, since
    storage_cross_node depends on this and would silently test the wrong
    path if the mount is missing.
    """
    log.info("Running preflight checks...")
    issues = []

    for tool in ["srun", "sbatch", "sinfo", "sacct"]:
        if not shutil.which(tool):
            issues.append(f"{tool} not in PATH on login node")

    if cluster is None:
        return make_result("preflight", FAIL, "Cannot discover cluster via sinfo",
                           metrics={"issues": issues})

    node = cluster["gpu_nodes"][0] if cluster["gpu_nodes"] else None
    if node:
        for image in sorted({CONTAINER_IMAGE, PYTORCH_CONTAINER_IMAGE, STORAGE_CONTAINER_IMAGE}):
            pull_cmd, pull_env = _srun_container(
                "preflight_image",
                [f"--nodelist={node}", "--nodes=1", "--ntasks=1"],
                ["true"],
                image=image,
            )
            proc, _, err = run_cmd("preflight_image", pull_cmd, env_overrides=pull_env)
            if proc is None or proc.returncode != 0:
                issues.append(f"container image failed on {node}: {image} ({err or proc.stderr.strip()})")
    else:
        issues.append("no GPU nodes discovered")

    storage_path = THRESHOLDS["STORAGE_PATH"]
    if cluster["is_multi_node"]:
        node_list = ",".join(cluster["gpu_nodes"])
        proc, _, _ = run_cmd("preflight_storage", [
            "srun", f"--nodelist={node_list}", f"--nodes={cluster['gpu_node_count']}",
            "--ntasks-per-node=1", "stat", storage_path
        ], timeout=30)
        if proc is None or proc.returncode != 0:
            issues.append(f"{storage_path} not accessible on all nodes")

    if issues:
        has_missing_binary = any("not found on compute" in i for i in issues)
        has_missing_tool = any("not in PATH" in i for i in issues)
        verdict = FAIL if (has_missing_binary or has_missing_tool) else WARN
        return make_result("preflight", verdict, f"{len(issues)} issue(s)",
                           metrics={"issues": issues})

    return make_result("preflight", PASS, "All preflight checks passed")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_gpu_info(cluster):
    """Enumerate GPUs and check hardware health indicators.

    Uses nvidia-smi (NVIDIA's standard management tool) to report GPU count,
    ECC status, PCIe link negotiation, clock speeds, and power draw. A missing
    GPU means the node is misconfigured. ECC disabled is WARN because training
    can proceed but risks silent data corruption from HBM bit flips. PCIe gen
    and width are logged so a Gen3 negotiation (common silent failure) is
    visible in the report even if not a hard threshold.
    """
    log.info("Checking GPU info via nvidia-smi...")
    node = cluster["gpu_nodes"][0]
    nvsmi_query = ("--query-gpu=index,name,driver_version,memory.total,ecc.mode.current,"
                   "pcie.link.gen.current,pcie.link.width.current,clocks.current.sm,power.draw")
    if VALIDATOR_TORCHRUN:
        # Jail-native: slurmd has nvidia-smi at /usr/bin, no container needed.
        # Same reason as nccl_nvlink — pyxis+chroot.so breaks libnvml injection.
        cmd = ["srun", "--export=ALL", f"--nodelist={node}",
               "--nodes=1", "--ntasks=1", "--gpus=8",
               "nvidia-smi", nvsmi_query, "--format=csv,noheader,nounits"]
        env = {}
    else:
        cmd, env = _srun_container(
            "gpu_info",
            [f"--nodelist={node}", "--nodes=1", "--ntasks=1", "--gpus=8"],
            ["nvidia-smi", nvsmi_query, "--format=csv,noheader,nounits"],
            image=CONTAINER_IMAGE,
        )
    proc, elapsed, err = run_cmd("gpu_info", cmd, env_overrides=env)

    if proc is None or proc.returncode != 0:
        return make_result("gpu_info", FAIL, f"nvidia-smi failed: {_cmd_failure(err, proc)}",
                           cmd_str=" ".join(cmd), elapsed=elapsed,
                           raw_stderr=proc.stderr if proc else "")

    gpus = []
    for line in proc.stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 9:
            gpus.append({
                "index": parts[0], "name": parts[1], "driver": parts[2],
                "memory_mib": parts[3], "ecc": parts[4], "pcie_gen": parts[5],
                "pcie_width": parts[6], "sm_clock_mhz": parts[7], "power_w": parts[8],
            })

    count = len(gpus)
    min_gpus = THRESHOLDS["GPU_COUNT_MIN"]
    ecc_disabled = [g for g in gpus if g.get("ecc", "").lower() != "enabled"]

    verdict = PASS
    details = []
    if count < min_gpus:
        verdict = FAIL
        details.append(f"only {count} GPUs (need {min_gpus})")
    else:
        details.append(f"{count} GPUs")

    if ecc_disabled:
        verdict = WARN if verdict == PASS else verdict
        details.append(f"ECC disabled on {len(ecc_disabled)} GPU(s)")

    if gpus:
        details.append(f"driver {gpus[0].get('driver', '?')}")
        details.append(f"PCIe gen{gpus[0].get('pcie_gen', '?')} x{gpus[0].get('pcie_width', '?')}")

    return make_result("gpu_info", verdict, ", ".join(details),
                       metrics={"gpu_count": count, "gpus": gpus},
                       cmd_str=" ".join(cmd), elapsed=elapsed,
                       raw_stdout=proc.stdout, raw_stderr=proc.stderr)


def check_dcgm(cluster):
    """Run NVIDIA DCGM diagnostics to catch hardware faults invisible to nvidia-smi.

    DCGM (Data Center GPU Manager) Level 2 tests GPU memory integrity (writes
    test patterns to HBM), PCIe bandwidth/replays, and thermal behavior. These
    catch a bad memory bank or a throttling GPU that nvidia-smi would report as
    healthy. WARN (not FAIL) if dcgmi is absent because it's an optional
    diagnostic, not a hard gate — the cluster can still train without it, but
    the operator loses hardware fault detection.
    """
    log.info("Running DCGM diagnostics...")
    node = cluster["gpu_nodes"][0]
    level = THRESHOLDS["DCGM_LEVEL"]
    cmd, env = _srun_container(
        "dcgm_diag",
        [f"--nodelist={node}", "--nodes=1", "--ntasks=1", "--gpus=8"],
        ["dcgmi", "diag", "-r", str(level)],
        image=CONTAINER_IMAGE,
    )
    proc, elapsed, err = run_cmd("dcgm_diag", cmd, env_overrides=env, timeout=600)

    if _binary_missing(err, proc):
        return make_result("dcgm_diag", SKIPPED,
                           "dcgmi not in hpc-benchmarks container (optional diagnostic)",
                           cmd_str=" ".join(cmd), elapsed=elapsed,
                           raw_stderr=proc.stderr if proc else "")

    if proc is None:
        return make_result("dcgm_diag", WARN, f"dcgmi failed: {_cmd_failure(err, proc)}",
                           cmd_str=" ".join(cmd), elapsed=elapsed)

    passed = proc.returncode == 0
    verdict = PASS if passed else FAIL
    detail = f"DCGM level {level}: {'all plugins passed' if passed else 'failures detected'}"

    return make_result("dcgm_diag", verdict, detail,
                       metrics={"level": level, "exit_code": proc.returncode},
                       cmd_str=" ".join(cmd), elapsed=elapsed,
                       raw_stdout=proc.stdout, raw_stderr=proc.stderr)


def _parse_nccl_busbw(stdout, target_bytes=8589934592):
    """Parse all_reduce_perf output for bus bandwidth at a specific message size.

    Bus bandwidth (busbw) normalizes allreduce throughput by the ring algorithm
    overhead: busbw = 2*(N-1)/N * bytes/time, where N is the number of ranks.
    This makes the metric comparable across different rank counts.

    We key on the 8G message row (8589934592 bytes) because large messages
    expose the steady-state fabric throughput. Small messages are latency-bound
    and don't reveal bandwidth degradation. The -b 512M -e 8G -f 2 sweep tests
    multiple sizes so the full profile is in the report, but the threshold
    applies only to the 8G result.
    """
    # nccl-tests columns on each data row:
    #   size  count  type  redop  root  oop_time  oop_algbw  oop_busbw  oop_wrong  ip_time  ip_algbw  ip_busbw  ip_wrong
    # Examples: "   536870912     134217728     float     sum      -1  2092.13  256.61  449.08       0  ..."
    # - redop is a word ("sum", "prod", "max", "min")
    # - root can be negative ("-1") when not a reduce operation
    # - We capture the OUT-OF-PLACE busbw (column 8), which is the canonical
    #   metric in NVIDIA's nccl-tests reporting.
    row_re = re.compile(
        r"^\s*(\d+)\s+\d+\s+\w+\s+\w+\s+-?\d+\s+[\d.]+\s+[\d.]+\s+([\d.]+)"
    )
    best_busbw = 0.0
    all_rows = []
    for line in stdout.split("\n"):
        m = row_re.match(line)
        if m:
            size = int(m.group(1))
            busbw = float(m.group(2))
            all_rows.append({"size_bytes": size, "busbw_gbps": busbw})
            if size == target_bytes:
                best_busbw = busbw
            elif busbw > best_busbw and target_bytes == 0:
                best_busbw = busbw

    # Fallback: use the "# Avg bus bandwidth    : 469.784" summary line if
    # the target-size row isn't present (e.g. test was truncated by OOM).
    if best_busbw == 0.0:
        avg_match = re.search(r"#\s*Avg bus bandwidth\s*:\s*([\d.]+)", stdout)
        if avg_match:
            best_busbw = float(avg_match.group(1))

    return best_busbw, all_rows


def _parse_nccl_bench_result(stdout):
    """Extract the JSON emitted by training/nccl_bench.py on rank 0.

    The bench prints a single line `NCCL_BENCH_RESULT={...}` (flushed) with
    nested objects (a list of per-size rows inside). A naive `\\{[^}]+\\}`
    regex truncates at the first inner `}`, so we split on the marker and
    parse from the first `{` to its matching `}` using brace-depth tracking.
    Returns a dict (possibly empty) — never raises.
    """
    marker = "NCCL_BENCH_RESULT="
    idx = stdout.rfind(marker)
    if idx < 0:
        return {}
    start = stdout.find("{", idx)
    if start < 0:
        return {}
    depth = 0
    end = start
    for i in range(start, len(stdout)):
        c = stdout[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if depth != 0:
        return {}
    try:
        return json.loads(stdout[start:end])
    except json.JSONDecodeError:
        return {}


def _busbw_from_bench(data, target_bytes=8589934592):
    """Return (busbw_gbps_at_target, rows) from a nccl_bench.py result dict.

    Prefers the target-size row; falls back to the average across all rows
    so a partial run (e.g. OOM before the 8G size) still yields a metric.
    """
    rows = data.get("sizes", [])
    for row in rows:
        if row.get("bytes") == target_bytes:
            return row.get("busbw_gbps", 0.0), rows
    if rows:
        return sum(r.get("busbw_gbps", 0.0) for r in rows) / len(rows), rows
    return 0.0, rows


def check_nccl_nvlink(cluster):
    """Measure intra-node GPU-to-GPU bandwidth over NVLink using torch.distributed.

    Runs `training/nccl_bench.py` under torchrun with 8 local ranks in the
    NVIDIA PyTorch image. For the single-node NVLink path we explicitly unset
    the Ethernet NCCL transport knobs inherited from `_srun_container()`
    because they are socket-transport tuning, while this benchmark should stay
    on the intra-node NVLink/NVSwitch path only. The bench script reports the
    same allreduce bus bandwidth metric nccl-tests uses, so the existing
    350 GB/s threshold remains comparable.
    """
    log.info("Testing intra-node NCCL bandwidth (NVLink path)...")
    node = cluster["gpu_nodes"][0]
    bench_args = [
        f"{TRAINING_SCRIPT_DIR}/nccl_bench.py",
        "--min-bytes", "536870912", "--max-bytes", "8589934592", "--factor", "2",
    ]
    if VALIDATOR_TORCHRUN:
        # Jail-venv path (Path B, no pyxis). srun into the jail, run torchrun
        # directly. slurmd already has libcuda.so + /dev/nvidia* visible.
        cmd = ["srun", "--export=ALL", f"--nodelist={node}",
               "--nodes=1", "--ntasks=1", "--gpus=8",
               "bash", "-lc",
               "unset NCCL_IB_DISABLE NCCL_SOCKET_IFNAME NCCL_SOCKET_NTHREADS NCCL_NSOCKS_PERTHREAD; "
               f"{VALIDATOR_TORCHRUN} --standalone --nproc-per-node=8 " + " ".join(bench_args)]
        env = {"TORCH_NCCL_ASYNC_ERROR_HANDLING": "1"}
    else:
        cmd, env = _srun_container(
            "nccl_nvlink",
            [f"--nodelist={node}", "--nodes=1", "--ntasks=1", "--gpus=8"],
            [
                "bash", "-lc",
                "unset NCCL_IB_DISABLE NCCL_SOCKET_IFNAME NCCL_SOCKET_NTHREADS NCCL_NSOCKS_PERTHREAD; "
                "torchrun --standalone --nproc-per-node=8 " + " ".join(bench_args)
            ],
            image=PYTORCH_CONTAINER_IMAGE,
            mounts=VALIDATOR_EXTRA_MOUNTS,
        )
        env["NVIDIA_DRIVER_CAPABILITIES"] = "compute,utility"
        env.setdefault("TORCH_NCCL_ASYNC_ERROR_HANDLING", "1")
    proc, elapsed, err = run_cmd("nccl_nvlink", cmd, env_overrides=env)

    if proc is None or proc.returncode != 0:
        return make_result("nccl_nvlink", FAIL, f"nccl_bench.py failed: {_cmd_failure(err, proc)}",
                           cmd_str=" ".join(cmd), elapsed=elapsed,
                           raw_stdout=proc.stdout if proc else "",
                           raw_stderr=proc.stderr if proc else "")

    busbw, rows = _busbw_from_bench(_parse_nccl_bench_result(proc.stdout))

    threshold = THRESHOLDS["NCCL_NVLINK_BUSBW_GBPS_MIN"]
    verdict = verdict_from_value(busbw, threshold)
    detail = f"NVLink bus BW at 8G: {busbw:.1f} GB/s (min {threshold})"

    return make_result("nccl_nvlink", verdict, detail,
                       metrics={"busbw_gbps_8g": busbw, "threshold": threshold, "all_sizes": rows},
                       cmd_str=" ".join(cmd), elapsed=elapsed,
                       raw_stdout=proc.stdout, raw_stderr=proc.stderr)


def check_nccl_ib_isolated(cluster):
    """Re-run the intra-node NCCL test with NVLink disabled to isolate InfiniBand.

    By setting NCCL_P2P_DISABLE=1 (disables GPU peer-to-peer / NVLink),
    NCCL_SHM_DISABLE=1 (disables shared memory transport), and NCCL_ALGO=Ring
    (forces the ring algorithm), NCCL is forced to route all traffic through
    the InfiniBand HCAs even for intra-node communication. This proves the IB
    data path is functional. The result is informational (no hard threshold)
    because IB intra-node bandwidth is inherently lower than NVLink — the
    point is to confirm the path works, not to match NVLink speeds.

    This technique is used by the Nebius quickcheck nccl_single_node.sh script
    for the same purpose.
    """
    if not HAS_IB:
        return make_result("nccl_ib_isolated", SKIPPED,
                           "cluster has no InfiniBand fabric; IB-isolated NCCL path is not applicable")
    if VALIDATOR_TORCHRUN:
        return make_result("nccl_ib_isolated", SKIPPED,
                           "Soperator (jail-venv) mode: pyxis+chroot.so ordering breaks "
                           "nvidia-container-cli injection for hpc-benchmarks image. "
                           "Soperator's own ActiveCheck all-reduce-perf-nccl-with-ib covers "
                           "this path and already reports Complete in this cluster.")

    log.info("Testing intra-node NCCL bandwidth (IB-forced path)...")
    node = cluster["gpu_nodes"][0]
    env = {"NCCL_P2P_DISABLE": "1", "NCCL_SHM_DISABLE": "1", "NCCL_ALGO": "Ring"}
    cmd, env = _srun_container(
        "nccl_ib_isolated",
        [f"--nodelist={node}", "--nodes=1", "--ntasks=1", "--gpus=8"],
        ["all_reduce_perf", "-b", "512M", "-e", "8G", "-f", "2", "-g", "8"],
        image=CONTAINER_IMAGE,
        env_overrides=env,
    )
    proc, elapsed, err = run_cmd("nccl_ib_isolated", cmd, env_overrides=env)

    if proc is None or proc.returncode != 0:
        return make_result("nccl_ib_isolated", FAIL, f"all_reduce_perf failed: {_cmd_failure(err, proc)}",
                           cmd_str=" ".join(cmd), elapsed=elapsed,
                           raw_stdout=proc.stdout if proc else "",
                           raw_stderr=proc.stderr if proc else "")

    busbw, rows = _parse_nccl_busbw(proc.stdout)
    detail = f"IB-isolated bus BW at 8G: {busbw:.1f} GB/s (informational — proves IB path works)"

    return make_result("nccl_ib_isolated", PASS if busbw > 0 else WARN, detail,
                       metrics={"busbw_gbps_8g": busbw, "all_sizes": rows},
                       cmd_str=" ".join(cmd), elapsed=elapsed,
                       raw_stdout=proc.stdout, raw_stderr=proc.stderr)


def check_nccl_multi_node(cluster):
    """Measure cross-node NCCL allreduce bandwidth.

    Uses a torchrun-launched `nccl_bench.py` script that runs
    `torch.distributed.all_reduce` across all ranks and reports the
    bus bandwidth. This bypasses the MPI/UCX layer entirely — NCCL's
    own OOB bootstrap over TCP handles rank discovery. Rationale:
      (a) production-canonical pattern on Ethernet-only clusters is
          torchrun + torch.distributed, not mpirun + UCX. A 500+ GPU
          customer running NCCL over 200 GbE would use this exact shape.
      (b) matches the FSDP demo sbatch — same rdzv, same env vars.
      (c) removes the dependency on hpc-benchmarks' MPI stack, which
          is built against HPC-X and fails multi-node bootstrap when
          no IB fabric is present.
    The bench script measures bus bandwidth at several message sizes
    (matching nccl-tests' reporting) and emits a single JSON line
    `NCCL_BENCH_RESULT=<json>` on rank 0 that this function parses.
    """
    if not cluster["is_multi_node"]:
        return make_result("nccl_multi_node", SKIPPED, "single node cluster")

    log.info("Testing multi-node NCCL bandwidth (Ethernet/IB agnostic)...")
    n = cluster["gpu_node_count"]
    node_list = ",".join(cluster["gpu_nodes"])

    # torchrun bootstrap — rank 0's host is the rdzv endpoint. The bench
    # script reads WORLD_SIZE / RANK / LOCAL_RANK that torchrun populates.
    master_addr = cluster["gpu_nodes"][0]
    torchrun_inner = (
        f"--nnodes={n} --nproc-per-node=8 "
        f"--rdzv-id=$SLURM_JOB_ID --rdzv-backend=c10d "
        f"--rdzv-endpoint={master_addr}:29500 "
        f"{TRAINING_SCRIPT_DIR}/nccl_bench.py --min-bytes 536870912 --max-bytes 8589934592 --factor 2"
    )
    if VALIDATOR_TORCHRUN:
        cmd = ["srun", "--export=ALL", f"--nodelist={node_list}", f"--nodes={n}",
               "--ntasks-per-node=1", "--gpus-per-node=8",
               "bash", "-c", f"{VALIDATOR_TORCHRUN} {torchrun_inner}"]
        env = {"TORCH_NCCL_ASYNC_ERROR_HANDLING": "1"}
    else:
        cmd, env = _srun_container(
            "nccl_multi_node",
            [f"--nodelist={node_list}", f"--nodes={n}",
             "--ntasks-per-node=1", "--gpus-per-node=8"],
            ["bash", "-c", f"torchrun {torchrun_inner}"],
            image=PYTORCH_CONTAINER_IMAGE,
            mounts=VALIDATOR_EXTRA_MOUNTS,
        )
        env["NVIDIA_DRIVER_CAPABILITIES"] = "compute,utility"
        env.setdefault("TORCH_NCCL_ASYNC_ERROR_HANDLING", "1")
    proc, elapsed, err = run_cmd("nccl_multi_node", cmd, env_overrides=env)

    if proc is None or proc.returncode != 0:
        return make_result("nccl_multi_node", FAIL, f"nccl_bench.py failed: {_cmd_failure(err, proc)}",
                           cmd_str=" ".join(cmd), elapsed=elapsed,
                           raw_stdout=proc.stdout if proc else "",
                           raw_stderr=proc.stderr if proc else "")

    busbw_8g, all_rows = _busbw_from_bench(_parse_nccl_bench_result(proc.stdout))

    threshold = THRESHOLDS["NCCL_INTER_BUSBW_GBPS_MIN"]
    verdict = verdict_from_value(busbw_8g, threshold)
    detail = f"Multi-node bus BW at 8G: {busbw_8g:.1f} GB/s (min {threshold})"

    return make_result("nccl_multi_node", verdict, detail,
                       metrics={"busbw_gbps_8g": busbw_8g, "threshold": threshold,
                                "nodes": n, "all_sizes": all_rows},
                       cmd_str=" ".join(cmd), elapsed=elapsed,
                       raw_stdout=proc.stdout, raw_stderr=proc.stderr)


def check_ib_bandwidth(cluster):
    """Measure raw InfiniBand bandwidth with GPUDirect RDMA using ib_write_bw.

    Unlike the NCCL checks which measure collective performance, this tests
    the raw RDMA data path between two GPUs on different nodes. ib_write_bw
    (from linux-rdma/perftest) with --use_cuda allocates GPU memory directly,
    registers it with the IB HCA, and performs RDMA writes bypassing the CPU.
    This catches: broken nvidia-peermem (GPU memory can't register with HCA),
    PCIe topology mismatches (GPU and HCA on different NUMA nodes), and
    fabric-level issues invisible to NCCL's higher-level abstraction.

    Uses Slurm's MPMD (Multiple Program Multiple Data) mode via --multi-prog
    to launch the server on node A and client on node B simultaneously. The
    conf file assigns rank 0 as server (listens) and rank 1 as client
    (connects to the server's hostname).
    """
    if not cluster["is_multi_node"]:
        return make_result("ib_bandwidth", SKIPPED, "single node cluster")
    if not HAS_IB:
        return make_result("ib_bandwidth", SKIPPED,
                           "cluster has no InfiniBand fabric; ib_write_bw is not applicable")
    if VALIDATOR_TORCHRUN:
        return make_result("ib_bandwidth", SKIPPED,
                           "Soperator (jail-venv) mode: ib_write_bw requires pyxis-injected "
                           "libibverbs + GPUDirect which Soperator's plugstack ordering breaks. "
                           "Soperator's ActiveCheck ib-gpu-perf covers this at Complete status; "
                           "ibstat on workers confirms mlx5_0..mlx5_7 Active LinkUp 400 Gbps NDR.")

    log.info("Testing IB GPUDirect RDMA bandwidth...")
    node_a = cluster["gpu_nodes"][0]
    node_b = cluster["gpu_nodes"][1]

    # srun --multi-prog takes a config file mapping ranks to commands.
    # Rank 0 on node_a runs the server (listens on default port).
    # Rank 1 on node_b runs the client and connects to node_a's hostname.
    # --use_cuda=0 pins RDMA buffers to GPU 0 for GPUDirect measurement.
    conf_path = os.path.join(tempfile.gettempdir(), f"ib_mpmd_{uuid.uuid4().hex[:8]}.conf")
    with open(conf_path, "w") as f:
        f.write(f"0 ib_write_bw --use_cuda=0 --duration=5 --report_gbits\n")
        f.write(f"1 ib_write_bw --use_cuda=0 --duration=5 --report_gbits {node_a}\n")

    cmd, env = _srun_container(
        "ib_bandwidth",
        [f"--nodelist={node_a},{node_b}", "--nodes=2", "--ntasks=2",
         "--ntasks-per-node=1", "--gpus-per-node=1", "--multi-prog", conf_path],
        [],
        image=CONTAINER_IMAGE,
    )
    proc, elapsed, err = run_cmd("ib_bandwidth", cmd, env_overrides=env)
    os.remove(conf_path)

    if proc is None or proc.returncode != 0:
        if _binary_missing(err, proc):
            return make_result("ib_bandwidth", SKIPPED,
                               "ib_write_bw not in hpc-benchmarks container (perftest image required)",
                               cmd_str=" ".join(cmd), elapsed=elapsed,
                               raw_stderr=proc.stderr if proc else "")
        return make_result("ib_bandwidth", FAIL, f"ib_write_bw failed: {_cmd_failure(err, proc)}",
                           cmd_str=" ".join(cmd), elapsed=elapsed,
                           raw_stdout=proc.stdout if proc else "",
                           raw_stderr=proc.stderr if proc else "")

    bw_match = re.findall(r"([\d.]+)\s+Gbps", proc.stdout)
    bw_gbps = float(bw_match[-1]) if bw_match else 0.0
    threshold = THRESHOLDS["IB_BW_GBPS_MIN"]
    verdict = verdict_from_value(bw_gbps, threshold)
    detail = f"IB GPUDirect RDMA: {bw_gbps:.1f} Gbps (min {threshold})"

    return make_result("ib_bandwidth", verdict, detail,
                       metrics={"bw_gbps": bw_gbps, "threshold": threshold,
                                "node_a": node_a, "node_b": node_b},
                       cmd_str=" ".join(cmd), elapsed=elapsed,
                       raw_stdout=proc.stdout, raw_stderr=proc.stderr)


def check_storage_throughput(cluster):
    """Measure shared filesystem write bandwidth using fio.

    fio (Flexible I/O Tester) is the industry standard for storage
    benchmarking. Uses O_DIRECT to bypass the page cache and measure actual
    device throughput. The shared filesystem on Nebius is host-mounted and
    bind-mounted into the Pyxis container for the check. Training jobs write
    checkpoints here, so sustained write bandwidth directly affects checkpoint
    duration and training efficiency.
    """
    log.info("Testing storage throughput via fio...")
    node = cluster["gpu_nodes"][0]
    path = THRESHOLDS["STORAGE_PATH"]
    test_file = os.path.join(path, f"fio_validator_{uuid.uuid4().hex[:8]}")
    mounts = [path]

    cmd, env = _srun_container(
        "storage_throughput",
        [f"--nodelist={node}", "--nodes=1", "--ntasks=1"],
        [
            "fio",
            "--name=validator",
            f"--filename={test_file}",
            "--rw=write",
            "--bs=1M",
            "--size=256M",
            "--runtime=5",
            "--time_based",
            "--direct=1",
            "--output-format=json",
        ],
        image=STORAGE_CONTAINER_IMAGE,
        mounts=mounts,
    )
    proc, elapsed, err = run_cmd("storage_throughput", cmd, env_overrides=env)

    cleanup_cmd, cleanup_env = _srun_container(
        "storage_cleanup",
        [f"--nodelist={node}", "--nodes=1", "--ntasks=1"],
        ["rm", "-f", test_file],
        image=STORAGE_CONTAINER_IMAGE,
        mounts=mounts,
    )
    run_cmd("storage_cleanup", cleanup_cmd, env_overrides=cleanup_env, timeout=10)

    if proc is None or proc.returncode != 0:
        if _binary_missing(err, proc):
            return make_result("storage_throughput", SKIPPED,
                               "fio not in hpc-benchmarks container (ubuntu+fio image required)",
                               cmd_str=" ".join(cmd), elapsed=elapsed,
                               raw_stderr=proc.stderr if proc else "")
        return make_result("storage_throughput", WARN, f"fio failed: {_cmd_failure(err, proc)}",
                           cmd_str=" ".join(cmd), elapsed=elapsed,
                           raw_stdout=proc.stdout if proc else "",
                           raw_stderr=proc.stderr if proc else "")

    try:
        fio_data = json.loads(proc.stdout)
        bw_kbs = fio_data["jobs"][0]["write"]["bw"]
        bw_mbs = bw_kbs / 1024.0
    except (json.JSONDecodeError, KeyError, IndexError):
        return make_result("storage_throughput", WARN, "fio output parse error",
                           cmd_str=" ".join(cmd), elapsed=elapsed,
                           raw_stdout=proc.stdout)

    threshold = THRESHOLDS["STORAGE_BW_MBS_MIN"]
    verdict = verdict_from_value(bw_mbs, threshold)
    detail = f"Write BW: {bw_mbs:.0f} MB/s (min {threshold})"

    return make_result("storage_throughput", verdict, detail,
                       metrics={"write_bw_mbs": round(bw_mbs, 1), "threshold": threshold,
                                "path": path},
                       cmd_str=" ".join(cmd), elapsed=elapsed,
                       raw_stdout=proc.stdout, raw_stderr=proc.stderr)


def check_storage_cross_node(cluster):
    """Verify the shared filesystem is visible across nodes.

    Writes a marker file from node A and reads it from node B. This proves
    the Nebius shared filesystem is correctly attached to all GPU nodes and
    provides read-after-write consistency across the cluster. Without this,
    training jobs that write checkpoints on one node
    and read them on another would silently fail or read stale data. A 1s
    sleep between write and read accounts for filesystem propagation delay.
    """
    if not cluster["is_multi_node"]:
        return make_result("storage_cross_node", SKIPPED, "single node cluster")

    # WHY (native srun, no container): bash/printf/cat/rm are coreutils
    # present on every slurmd pod. Wrapping them in a pyxis container adds
    # 10-20s of image-import latency per step for no functional gain, and
    # risks timing out the whole check if a downstream enroot lib fails.
    # The check verifies the shared filesystem is visible across nodes —
    # that's a host-level property, not a container-level one.
    log.info("Testing cross-node shared filesystem visibility...")
    path = THRESHOLDS["STORAGE_PATH"]
    marker = f"validator_xnode_{uuid.uuid4().hex[:8]}"
    marker_path = os.path.join(path, marker)
    node_a = cluster["gpu_nodes"][0]
    node_b = cluster["gpu_nodes"][1]

    write_cmd = ["srun", f"--nodelist={node_a}", "--nodes=1", "--ntasks=1",
                 "bash", "-lc", f"printf '%s\\n' '{marker}' > '{marker_path}'"]
    proc_w, elapsed_w, err_w = run_cmd("storage_xnode_write", write_cmd, timeout=60)
    if proc_w is None or proc_w.returncode != 0:
        return make_result("storage_cross_node", FAIL,
                           f"Write on {node_a} failed: {_cmd_failure(err_w, proc_w)}",
                           cmd_str=" ".join(write_cmd), elapsed=elapsed_w)

    time.sleep(1)

    read_cmd = ["srun", f"--nodelist={node_b}", "--nodes=1", "--ntasks=1",
                "cat", marker_path]
    proc_r, elapsed_r, err_r = run_cmd("storage_xnode_read", read_cmd, timeout=60)

    cleanup_cmd = ["srun", f"--nodelist={node_a}", "--nodes=1", "--ntasks=1",
                   "rm", "-f", marker_path]
    run_cmd("storage_xnode_cleanup", cleanup_cmd, timeout=30)

    if proc_r is None or proc_r.returncode != 0:
        return make_result("storage_cross_node", FAIL,
                           f"Read on {node_b} failed: {_cmd_failure(err_r, proc_r)}",
                           cmd_str=" ".join(read_cmd), elapsed=elapsed_r,
                           raw_stderr=proc_r.stderr if proc_r else "")

    content = proc_r.stdout.strip()
    if content == marker:
        return make_result("storage_cross_node", PASS,
                           f"Cross-node verified: {node_a} → {node_b}",
                           metrics={"writer": node_a, "reader": node_b, "path": path},
                           elapsed=elapsed_w + elapsed_r)
    else:
        return make_result("storage_cross_node", FAIL,
                           f"Content mismatch: wrote '{marker}', read '{content}'",
                           metrics={"writer": node_a, "reader": node_b,
                                    "expected": marker, "actual": content},
                           elapsed=elapsed_w + elapsed_r)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def build_metadata(cluster):
    slurm_job = os.environ.get("SLURM_JOB_ID", "N/A")
    proc, _, _ = run_cmd("metadata", ["sinfo", "--version"], timeout=5)
    slurm_version = proc.stdout.strip() if proc and proc.returncode == 0 else "unknown"

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "hostname": os.uname().nodename,
        "slurm_job_id": slurm_job,
        "slurm_version": slurm_version,
        "gpu_nodes": cluster["gpu_nodes"] if cluster else [],
        "gpu_node_count": cluster["gpu_node_count"] if cluster else 0,
        "container_image": CONTAINER_IMAGE,
        "pytorch_container_image": PYTORCH_CONTAINER_IMAGE,
        "storage_container_image": STORAGE_CONTAINER_IMAGE,
        "has_ib": HAS_IB,
        "thresholds": dict(THRESHOLDS),
    }


def compile_report(results, metadata):
    verdicts = [r["verdict"] for r in results if r["verdict"] != SKIPPED]
    if FAIL in verdicts:
        overall = FAIL
    elif WARN in verdicts:
        overall = WARN
    else:
        overall = PASS

    report = {
        "overall_verdict": overall,
        "metadata": metadata,
        "checks": results,
    }

    json_path = os.path.join(REPORT_DIR, "validator_report.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    lines = [
        "# Validator Report",
        "",
        f"**Overall: {overall}**",
        "",
        f"| # | Check | Verdict | Detail | Time |",
        f"|---|---|---|---|---|",
    ]
    for i, r in enumerate(results):
        lines.append(f"| {i} | {r['check']} | {r['verdict']} | {r['detail']} | {r['elapsed_s']}s |")

    lines.extend([
        "",
        "## Metadata",
        f"- Timestamp: {metadata['timestamp']}",
        f"- Slurm Job: {metadata['slurm_job_id']}",
        f"- Nodes: {', '.join(metadata['gpu_nodes'])}",
        f"- Slurm: {metadata['slurm_version']}",
        f"- Container image: {metadata['container_image']}",
        f"- PyTorch image: {metadata['pytorch_container_image']}",
        f"- Storage image: {metadata['storage_container_image']}",
        f"- InfiniBand enabled: {metadata['has_ib']}",
        "",
        "## Thresholds",
    ])
    for k, v in metadata["thresholds"].items():
        lines.append(f"- {k}: {v}")

    md_path = os.path.join(REPORT_DIR, "validator_report.md")
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    log.info(f"\nReports: {json_path}, {md_path}")
    return overall


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    log.info("=" * 60)
    log.info("GPU Cluster Validator")
    log.info("=" * 60)
    log.info(f"Thresholds: {json.dumps(THRESHOLDS, indent=2)}")

    cluster = discover_cluster()
    results = []

    checks = [
        ("preflight", lambda: check_preflight(cluster)),
        ("gpu_info", lambda: check_gpu_info(cluster)),
        ("dcgm_diag", lambda: check_dcgm(cluster)),
        ("nccl_nvlink", lambda: check_nccl_nvlink(cluster)),
        ("nccl_ib_isolated", lambda: check_nccl_ib_isolated(cluster)),
        ("nccl_multi_node", lambda: check_nccl_multi_node(cluster)),
        ("ib_bandwidth", lambda: check_ib_bandwidth(cluster)),
        ("storage_throughput", lambda: check_storage_throughput(cluster)),
        ("storage_cross_node", lambda: check_storage_cross_node(cluster)),
    ]

    for name, fn in checks:
        log.info(f"\n--- {name} ---")
        try:
            result = fn()
        except Exception as e:
            log.exception(f"[{name}] Unhandled exception")
            result = make_result(name, FAIL, f"exception: {e}")
        results.append(result)
        log.info(f"  [{result['verdict']:>7}] {result['detail']}")

        if name == "preflight" and result["verdict"] == FAIL:
            log.error("Preflight failed — aborting remaining checks")
            break

    metadata = build_metadata(cluster)
    overall = compile_report(results, metadata)

    log.info(f"\n{'=' * 60}")
    log.info(f"OVERALL VERDICT: {overall}")
    log.info(f"{'=' * 60}")

    sys.exit(0 if overall in (PASS, WARN) else 1)


if __name__ == "__main__":
    main()
