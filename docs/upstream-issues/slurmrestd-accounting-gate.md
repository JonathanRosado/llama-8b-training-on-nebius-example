# Soperator 3.0.2: `slurmrestd` is silently gated on accounting as well as `rest.enabled`

## Summary

In Soperator 3.0.2, enabling the REST surface is not controlled by `rest.enabled=true` alone. The controller short-circuits `slurmrestd` reconciliation unless accounting or DB support is also enabled. In our PoC this produced a misleading failure mode: Terraform waited forever on ActiveChecks even though the direct root cause was the hidden REST/accounting dependency.

## Environment

- Soperator 3.0.2
- Self-deployed Path B cluster under `soperator/installations/poc/`
- `rest.enabled=true`
- `accounting_enabled=false` in the first apply attempt

## Observed behavior

- `wait_for_soperator_activechecks_hr` looped indefinitely.
- `slurmrestd` was not reconciled.
- ActiveChecks that depend on the Slurm REST API never reached `Complete`.
- Repo evidence:
  - `CHANGELOG.md:320-321`
  - `soperator/installations/example/variables.tf:1102-1148`

## Expected behavior

If REST requires accounting, the operator should fail loudly and immediately when `rest.enabled=true` but accounting is disabled. The current silent skip looks like a chart/controller defect even if the dependency is intentional.

## Impact

- Adds hidden infrastructure requirements to minimal PoC installs.
- Forced this PoC to enable accounting plus a 64 GiB filestore even though accounting was not part of the intended feature scope.
- Increases bring-up time and obscures the real reason ActiveChecks are blocked.

## Workaround used in this PoC

- Set `accounting_enabled=true`
- Provision `filestore_accounting` at 64 GiB
- Re-apply until REST and accounting pods reconcile
- Repo evidence:
  - `CHANGELOG.md:320-321`

## Requested upstream action

1. If this dependency is by design, document it prominently in install docs and variable descriptions.
2. Add validation that rejects `rest.enabled=true` with accounting disabled.
3. Surface a controller event or condition explaining that REST was skipped because accounting/DB support is off.
