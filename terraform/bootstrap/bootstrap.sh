#!/usr/bin/env bash
#
# terraform/bootstrap/bootstrap.sh
#
# Pre-stage the Nebius identity setup for this repo. Idempotent — safe to
# re-run any number of times.
#
# There are two identity lanes (see IDENTITY.md):
#
#   1. Human lane  — the federated CLI profile (`rosadoft`), used for
#                    interactive Day-1 work: Terraform plan/apply from a
#                    workstation, CLI inspection, inventory probes.
#   2. Automation lane — the `terraform-admin` service account, used for
#                    anything that needs a credential that isn't a human
#                    in front of a browser: Soperator running inside K8s,
#                    Slurm elastic scaling glue, CI-driven Terraform, etc.
#                    The automation lane has to exist before the first
#                    Soperator / K8s / Slurm workload lands.
#
# This script sets up everything it can for the automation lane under the
# CURRENT (human) CLI profile:
#
#   1. Creates the `terraform-admin` service account in the project.
#   2. Places it in the tenant `editors` group so it inherits the same
#      project-write scope Nebius already grants its own seeded SAs.
#   3. If an SA credentials file already exists locally, creates / verifies
#      the `nebius-terraform` CLI profile bound to it. Otherwise, skips the
#      profile step cleanly — credential provisioning is a separate elevated
#      flow (see IDENTITY.md "Activating the automation lane").
#
# Requires: nebius CLI on PATH or at ~/.nebius/bin, python3.

set -euo pipefail

# -------- config --------
SA_NAME="terraform-admin"
PROFILE_NAME="nebius-terraform"
CREDS_DIR="${HOME}/.nebius-sa-creds"
CREDS_FILE="${CREDS_DIR}/${SA_NAME}.json"
GROUP_NAME="editors"

# -------- pretty output --------
log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[bootstrap]\033[0m %s\n' "$*" >&2; exit 1; }

# -------- prerequisites --------
export PATH="${HOME}/.nebius/bin:${PATH}"
command -v nebius  >/dev/null || die "nebius CLI not found (tried PATH and ~/.nebius/bin)"
command -v python3 >/dev/null || die "python3 not found (required for json parsing)"

# Single scratch dir for this run, auto-cleaned on exit.
SCRATCH="$(mktemp -d -t nebius-bootstrap.XXXXXX)"
trap 'rm -rf "${SCRATCH}"' EXIT

# pyjq <python-expression-on-`d`>: read stdin as JSON into variable `d`, eval
# a pure-Python expression on it, print the result. The expression must NOT
# embed shell values — pass them via environment variables and read with
# os.environ inside the expression. This keeps the helper safe against
# injection via IDs that contain quotes.
pyjq() {
  python3 -c '
import json, os, sys
d = json.load(sys.stdin)
r = eval(sys.argv[1], {"d": d, "os": os})
if isinstance(r, list):
    print("\n".join("" if x is None else str(x) for x in r))
elif r is not None:
    print(r)
' "$1"
}

PROJECT_ID="$(nebius config get parent-id)"
TENANT_ID="$(nebius config get tenant-id)"
[[ -n "${PROJECT_ID}" ]] || die "nebius config has no parent-id (run under a federated profile first)"
[[ -n "${TENANT_ID}" ]]  || die "nebius config has no tenant-id"

log "project-id = ${PROJECT_ID}"
log "tenant-id  = ${TENANT_ID}"

# -------- step 1: service account --------
log "resolving service account '${SA_NAME}'..."
SA_ID="$(SA_NAME="${SA_NAME}" nebius iam service-account list --parent-id "${PROJECT_ID}" --format json \
  | SA_NAME="${SA_NAME}" pyjq "next((it['metadata']['id'] for it in d.get('items', []) if it.get('metadata', {}).get('name')==os.environ['SA_NAME']), None)" \
  || true)"

if [[ -z "${SA_ID}" ]]; then
  log "creating service account '${SA_NAME}'..."
  SA_ID="$(nebius iam service-account create \
    --name "${SA_NAME}" \
    --description "Terraform/automation-lane identity" \
    --parent-id "${PROJECT_ID}" \
    --format json \
    | pyjq "d.get('metadata', {}).get('id')")"
  [[ -n "${SA_ID}" ]] || die "service-account create returned no id"
  log "created ${SA_ID}"
else
  log "re-using existing ${SA_ID}"
fi

# -------- step 2: editors group membership --------
log "resolving '${GROUP_NAME}' group in tenant..."
GROUP_ID="$(GROUP_NAME="${GROUP_NAME}" nebius iam group list --parent-id "${TENANT_ID}" --format json \
  | GROUP_NAME="${GROUP_NAME}" pyjq "next((it['metadata']['id'] for it in d.get('items', []) if it.get('metadata', {}).get('name')==os.environ['GROUP_NAME']), None)" \
  || true)"
[[ -n "${GROUP_ID}" ]] || die "could not find group '${GROUP_NAME}' in tenant ${TENANT_ID}"
log "group-id   = ${GROUP_ID}"

# list-members wraps items under `memberships` (unlike most list endpoints
# which use `items`), so handle both shapes.
log "checking whether ${SA_ID} is already a member of '${GROUP_NAME}'..."
ALREADY_MEMBER="$(SA_ID="${SA_ID}" nebius iam group-membership list-members --parent-id "${GROUP_ID}" --format json 2>/dev/null \
  | SA_ID="${SA_ID}" pyjq "
next((
    it['metadata']['id']
    for it in (d.get('memberships') or d.get('items') or [])
    if isinstance(it, dict) and it.get('spec', {}).get('member_id') == os.environ['SA_ID']
), None)
" || true)"

if [[ -z "${ALREADY_MEMBER}" ]]; then
  log "adding ${SA_ID} to group ${GROUP_NAME}..."
  nebius iam group-membership create \
    --parent-id "${GROUP_ID}" \
    --member-id "${SA_ID}" \
    --format json >/dev/null
  log "membership created"
else
  log "already a member — skipping"
fi

# -------- step 3: CLI profile for the automation lane (conditional) --------
#
# We only wire up the `nebius-terraform` CLI profile when a credentials file
# for the SA already exists locally. Minting the credential itself is a
# separate elevated step (see IDENTITY.md) — this script deliberately does
# not attempt it, because on this tenant the human-lane identity does not
# hold the permission required to call AuthPublicKeyService.Create or
# AccessKeyService.Create for the SA.
#
# When the credentials file appears at ${CREDS_FILE}, re-run this script
# and the profile is created.

mkdir -p "${CREDS_DIR}"
chmod 700 "${CREDS_DIR}"

if [[ -s "${CREDS_FILE}" ]]; then
  log "found credentials at ${CREDS_FILE} — wiring '${PROFILE_NAME}' profile"
  chmod 600 "${CREDS_FILE}"

  if nebius profile list 2>/dev/null | awk '{print $1}' | grep -Fxq "${PROFILE_NAME}"; then
    log "profile '${PROFILE_NAME}' already exists — skipping profile create"
  else
    nebius profile create "${PROFILE_NAME}" \
      --service-account-file "${CREDS_FILE}" \
      --parent-id "${PROJECT_ID}" \
      --tenant-id "${TENANT_ID}" \
      --skip-auth
    log "profile '${PROFILE_NAME}' created"
  fi

  log "verifying with 'nebius --profile ${PROFILE_NAME} iam whoami'..."
  WHOAMI="$(nebius --profile "${PROFILE_NAME}" iam whoami --format json 2>&1)" || die "whoami failed: ${WHOAMI}"
  WHOAMI_SA_ID="$(printf '%s' "${WHOAMI}" | pyjq 'd.get("service_account_profile", {}).get("id")' || true)"
  if [[ -n "${WHOAMI_SA_ID}" ]]; then
    log "OK — '${PROFILE_NAME}' authenticates as ${WHOAMI_SA_ID}"
    [[ "${WHOAMI_SA_ID}" == "${SA_ID}" ]] || warn "whoami id does not match created SA id"
    LANE_ACTIVE=1
  else
    die "whoami verification failed (no service_account_profile in response): ${WHOAMI}"
  fi
else
  LANE_ACTIVE=0
fi

# -------- summary --------
cat <<EOF

-----------------------------------------------------------
identity setup — summary

  project:    ${PROJECT_ID}
  tenant:     ${TENANT_ID}

  human lane: ${LANE_HUMAN:-rosadoft} (federated)                    [active]
  automation lane:
    service-account: ${SA_NAME} (${SA_ID})                          [pre-staged]
    group:           ${GROUP_NAME} (${GROUP_ID})                    [joined]
    credentials:     ${CREDS_FILE}$( [[ "${LANE_ACTIVE:-0}" == "1" ]] && echo "    [active]" || echo "    [not yet provisioned]" )
    CLI profile:     ${PROFILE_NAME}$( [[ "${LANE_ACTIVE:-0}" == "1" ]] && echo "                       [active]" || echo "                       [not yet created]" )

Next:

  - Run any Day-1 Terraform / CLI work under the human lane:
      cd terraform/poc && terraform init && terraform plan

  - The automation lane becomes required the first time Soperator, Slurm,
    or in-cluster Kubernetes needs to authenticate on its own. See
    IDENTITY.md → "Activating the automation lane" for the activation flow.
    Once a credentials file is at ${CREDS_FILE}, re-run this script and
    the '${PROFILE_NAME}' CLI profile will wire up automatically.
-----------------------------------------------------------
EOF
