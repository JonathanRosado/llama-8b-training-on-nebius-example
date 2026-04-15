# Soperator docs gap: no documented `jail_base_image` or equivalent path for Python tooling inside the jail

## Summary

The live Path B PoC needed additional Python packages inside the Slurm jail (`torch`, `transformers`, related runtime tooling) to run training directly from the jail. The documented containerized answer would normally be Pyxis, but Pyxis is broken on this cluster due to the `chroot.so` / `spank_pyxis.so` interaction documented separately. The repo does not show a documented, supported way to customize the jail base image or pre-bake additional Python dependencies for this case.

## Environment

- Soperator 3.0.2
- Self-deployed Path B cluster at `soperator/installations/poc/`
- Jail / virtiofs execution model
- Torch workloads launched from `/home/nebius/venv`

## Observed behavior

- The effective workaround was to create a jail-resident virtualenv and install torch there.
- Repo evidence:
  - `validator/validate.py:104-110`
  - `soperator/installations/poc/fsdp_demo.sbatch:51-56`
  - `CHANGELOG.md:324-326`

## Expected behavior

There should be a documented and supported mechanism for one of these patterns:

1. Override or extend the jail base image/rootfs with additional packages.
2. Pre-seed a supported Python environment into the jail at cluster creation time.
3. Clearly state that the supported customization path is a user-managed venv in the jail, if that is the intended answer.

## Impact

- Leaves operators guessing how to install training dependencies when Pyxis is unavailable.
- Makes the jail workaround look ad hoc even though it is the only working path on this PoC.
- Weakens reproducibility for customer handoff.

## Requested upstream action

1. Document the supported way to add Python runtime dependencies to the jail.
2. If a `jail_base_image`-style override exists, add it to the installation examples.
3. If no such override exists, document the venv-in-jail pattern as an explicit fallback for torch-based workflows.
