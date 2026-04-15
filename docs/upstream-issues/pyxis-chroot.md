# Soperator 3.0.2: `chroot.so` ordering breaks Pyxis / enroot NVIDIA injection in jail mode

## Summary

On the live Path B cluster (`eu-north2`, Soperator 3.0.2), nested Pyxis execution from the Slurm jail fails to expose a usable NVIDIA userspace inside the container. The observed pattern is that `chroot.so` is applied before `spank_pyxis.so`, and enroot's `98-nvidia.sh` hook does not leave `libnvidia-ml.so` and `/dev/nvidia*` correctly present in the final Pyxis rootfs.

## Environment

- Soperator 3.0.2, self-deployed via `soperator/installations/poc/`
- Region: `eu-north2`
- GPU platform: H200 SXM
- Slurm jail / virtiofs shared root
- Pyxis + enroot enabled on the Soperator stack

## Observed behavior

- Pyxis container launch path executes, but PyTorch inside the container reports no NVIDIA driver.
- Repo evidence:
  - `validator/validate.py:102-110`
  - `CHANGELOG.md:322-326`
- Concrete failure captured in repo notes:
  - `RuntimeError: Found no NVIDIA driver on your system`

## Expected behavior

When `srun --container-image=...` is used on a GPU allocation, the resulting container should see the same working GPU userspace that is already available from the jail-resident process environment.

## Impact

- Breaks the documented Pyxis-based path for Torch workloads on Path B.
- Forces a workaround that bypasses Pyxis entirely and runs `/home/nebius/venv/bin/torchrun` inside the jail.
- Reduces confidence that containerized training jobs can use the same path as non-containerized ActiveChecks.

## Workaround used in this PoC

- Do not use Pyxis for torch-based jobs on Path B.
- Install torch into a jail-resident virtualenv and run directly from the jail.
- Repo evidence:
  - `validator/validate.py:104-110`
  - `soperator/installations/poc/fsdp_demo.sbatch:51-56`
  - `CHANGELOG.md:324-326`

## Requested upstream action

One of the following:

1. Document the supported plugstack ordering for `chroot.so` and `spank_pyxis.so` when GPU containers are launched from the jail.
2. If the current ordering is unintended, fix the ordering or the NVIDIA injection path so enroot sees the correct root view.
3. Add an explicit ActiveCheck or smoke test that validates `torch.cuda.is_available()` from a Pyxis container in jail mode.
