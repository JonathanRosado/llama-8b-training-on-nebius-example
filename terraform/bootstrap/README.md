# terraform/bootstrap/

Pre-stages the Nebius identity setup this repo uses. Run it once now,
under your federated CLI profile, and re-run it any time you add the
automation-lane credentials file or want to confirm state.

See [`IDENTITY.md`](./IDENTITY.md) for the full two-lane identity model,
when to use each lane, and how to activate the automation lane.

## What it does

1. Creates a `terraform-admin` service account in the current project
   (skips if it already exists).
2. Adds it to the tenant-seeded `editors` group so it inherits the same
   project-write scope Nebius already grants its own pre-provisioned
   service accounts (skips if already a member).
3. If a credentials file exists at
   `~/.nebius-sa-creds/terraform-admin.json`, creates a Nebius CLI
   profile `nebius-terraform` bound to it and verifies with `iam whoami`.
4. If that file does not yet exist, stops cleanly after step 2 and
   prints the next step from IDENTITY.md. Minting the credential is a
   separate elevated flow, documented in IDENTITY.md → *Activating the
   automation lane*.

All steps are idempotent — re-running is always safe.

## Why split credential minting out

The human-lane identity (federated Google user in the tenant `admins`
group) does not hold the project-scoped
`iam.authPublicKey.create` / `iam.accessKey.create` permissions needed
to provision the SA's credential. This is deliberate in how Nebius
carved the default roles — the four SAs that already exist in `editors`
were all minted through the same elevated flow. So the bootstrap script
does the parts it can (SA + group membership + local profile wiring)
and leaves the credential mint to the documented elevated path.

This is **not** a blocker for Day 1: Terraform and the CLI work fine
against the federated human-lane profile. The automation lane's
credential only becomes required the first time a Soperator / Slurm /
Kubernetes workload needs an in-cluster identity, at which point you
follow IDENTITY.md → *Activating the automation lane*.

## Running it

```bash
# from repo root, with your federated profile active
bash terraform/bootstrap/bootstrap.sh
```

Expected summary block at the end lists both identity lanes with a
per-row state tag. Pre-credential, you'll see
`[pre-staged]` / `[not yet provisioned]` on the automation-lane rows;
post-activation, you'll see `[active]`.

## Teardown

If you need to remove everything the script created:

```bash
#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.nebius/bin:$PATH"

# small helper: read stdin as JSON, eval an expression on `d`, print the result
pyq() { python3 -c 'import json,sys; d=json.load(sys.stdin); print('"$1"' or "")'; }

PROJECT_ID=$(nebius config get parent-id)
TENANT_ID=$(nebius config get tenant-id)

# 1. Remove the CLI profile (local only)
nebius profile delete nebius-terraform 2>/dev/null || true

# 2. Delete the private credentials file (if it was ever provisioned)
rm -f ~/.nebius-sa-creds/terraform-admin.json

# 3. Find the SA and the group
SA_ID=$(nebius iam service-account list --parent-id "$PROJECT_ID" --format json \
  | pyq 'next((it["metadata"]["id"] for it in d.get("items",[]) if it.get("metadata",{}).get("name")=="terraform-admin"), None)')
GROUP_ID=$(nebius iam group list --parent-id "$TENANT_ID" --format json \
  | pyq 'next((it["metadata"]["id"] for it in d.get("items",[]) if it.get("metadata",{}).get("name")=="editors"), None)')

# 4. Remove the group membership
MEMBERSHIP_ID=$(nebius iam group-membership list-members --parent-id "$GROUP_ID" --format json \
  | SA_ID="$SA_ID" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
sa = os.environ["SA_ID"]
for it in (d.get("memberships") or d.get("items") or []):
    if it.get("spec", {}).get("member_id") == sa and it.get("metadata", {}).get("id"):
        print(it["metadata"]["id"])
        break
')
[ -n "$MEMBERSHIP_ID" ] && nebius iam group-membership delete --id "$MEMBERSHIP_ID"

# 5. Delete any auth public key on the SA (none exists pre-activation)
KEY_ID=$(nebius iam auth-public-key list-by-account --account-service-account-id "$SA_ID" --format json \
  | pyq 'next((it["metadata"]["id"] for it in d.get("items",[])), None)')
[ -n "$KEY_ID" ] && nebius iam auth-public-key delete --id "$KEY_ID"

# 6. Delete the service account
nebius iam service-account delete --id "$SA_ID"
```

## Prerequisites

- `nebius` CLI on PATH or at `~/.nebius/bin/nebius`
- `python3` (stdlib only — no jq, no third-party deps)
- An active federated Nebius CLI profile with permission to create
  service accounts and group memberships in the target tenant
