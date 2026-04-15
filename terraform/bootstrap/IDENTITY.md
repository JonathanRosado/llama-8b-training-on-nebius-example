# Identity model

This repo uses two identity lanes for authenticating against Nebius, by
design. Both are legitimate, both are needed, and they exist for
different workloads. Day-1 work runs on the human lane; the automation
lane is pre-staged and activates when Soperator / Slurm / Kubernetes
brings in workloads that cannot hold a human-issued token.

## The two lanes

### Human lane — federated CLI profile

- Subject: the federated Google user (`rosadoft` on this workstation).
- Credential: short-lived IAM token minted from a browser-based OAuth
  flow, cached by the Nebius CLI at `~/.nebius/config.yaml`.
- Scope of use: **anything a human runs in front of a terminal** —
  Terraform `plan` / `apply` from a workstation, `nebius` CLI inspection,
  read-only inventory probes, one-shot experiments, Day-1 PoC bring-up.
- Limits: the token is short-lived and tied to a browser session. It
  cannot be embedded in a CI pipeline variable, mounted into a
  Kubernetes Secret, or handed to a controller running inside a cluster.
  That's fine — those workloads don't belong on this lane.
- **This is Nebius's documented pattern for a solo operator on a fresh
  tenant** — see the Terraform provider quickstart, which explicitly
  lists user-account auth alongside service-account auth.

### Automation lane — `terraform-admin` service account

- Subject: a Nebius service account named `terraform-admin`, parented
  under the PoC project. Member of the tenant-seeded `editors` group so
  it inherits the same project-write scope Nebius already grants its own
  pre-provisioned SAs (there are four of them alongside ours).
- Credential: an asymmetric auth public key pair. The private half lives
  at `~/.nebius-sa-creds/terraform-admin.json` (chmod 600, outside the
  repo, never committed). The public half is uploaded to Nebius as an
  `iam.authPublicKey` resource on the SA.
- Scope of use: **anything that has no human in the loop** — Soperator
  controllers inside Managed Kubernetes, Slurm elastic scaling hooks, CI
  pipelines that run `terraform apply`, background jobs that refresh
  checkpoints, scheduled validators. Nothing on this lane should require
  a browser.
- State today: the SA and group membership are already created by
  `bootstrap.sh`. The credential itself is not yet minted — see
  *Activating the automation lane* below.

## When to use which

| Workload | Lane |
|---|---|
| `terraform plan` / `apply` from a workstation, during Day-1 PoC | Human |
| `nebius` CLI interactive exploration | Human |
| `inventory/state_dump.py` probes | Human |
| Terraform running from CI | Automation |
| Soperator operator pod inside the K8s cluster | Automation |
| Slurm elastic autoscaling glue that calls Nebius APIs | Automation |
| Container image builds pushing to Nebius Container Registry | Automation |
| Nightly validator / benchmark jobs | Automation |

Anything in the bottom half of that table needs the automation lane
activated first — which is the trigger to run the activation flow below.

## Activating the automation lane

The automation lane's credential (the SA's auth public key) is minted by
an **elevated IAM principal**, not by the federated human lane. This is
consistent with how the tenant was provisioned: the human lane's `admin`
role on the tenant explicitly does not include the project-scoped
`iam.authPublicKey.create` / `iam.accessKey.create` permissions, and
that split is deliberate — the four pre-seeded SAs already in the
`editors` group were minted through the same elevated flow.

The activation is a one-shot manual step and lands a JSON credentials
file at `~/.nebius-sa-creds/terraform-admin.json`. Two ways to do it:

1. **Nebius console UI** — navigate to the `terraform-admin` SA in the
   IAM → Service Accounts view, add an authorized key, and download the
   generated JSON. Save it to `~/.nebius-sa-creds/terraform-admin.json`,
   `chmod 600`.
2. **Nebius support ticket** — if the console UI route isn't available
   in your tenant, open a ticket with the template in
   *Support ticket template* below. Support provisions the credential,
   you receive the JSON, same `chmod 600` placement.

After the credentials file is in place, re-run `bootstrap.sh`. It
detects the file and creates the `nebius-terraform` CLI profile bound to
it, then verifies with `nebius --profile nebius-terraform iam whoami`.
No other changes needed — the SA is already in `editors`, and the
Terraform variable is one line away:

```bash
sed -i 's/profile_name = "rosadoft"/profile_name = "nebius-terraform"/' \
    terraform/poc/poc.auto.tfvars
cd terraform/poc
terraform init -reconfigure
terraform plan
```

## Support ticket template

> Tenant: `tenant-e00zr420cdr0rzv9br` (csa-hiring-sandboxO)
> Project: `project-e02qt8kdpr00x4ag7thr64`
> Requester: rosadoft@gmail.com (member of tenant `admins` group)
>
> Please mint an authorized key for service account `terraform-admin`
> (`serviceaccount-e00w24303v8yy7ervg`), which is already a member of the
> tenant `editors` group. The human lane (Google-federated admin user)
> does not hold `iam.authPublicKey.create` or `iam.accessKey.create`
> scoped to the project, so credential provisioning needs to happen via
> an elevated flow. Return the SA credentials JSON file or point me at
> the console UI path for the same operation.
>
> Reason: the PoC is about to bring up Soperator on Managed Kubernetes
> (and/or Slurm elastic glue), which requires an in-cluster automation
> identity.

Include a recent `Trace ID` from a failed CLI run if support asks for
one (they're shown in every `PermissionDenied` error from the probe).

## Reproducing the permission boundary

If at some point you want to re-verify that the human lane truly can't
mint the credential — e.g. before opening the support ticket, to be
sure nothing has shifted under you — the probe is in your shell history
or can be rebuilt from this block:

```bash
export PATH="$HOME/.nebius/bin:$PATH"
SA=serviceaccount-e00w24303v8yy7ervg
PROJECT=$(nebius config get parent-id)

# Should return PermissionDenied on AuthPublicKeyService.Create
nebius iam auth-public-key generate \
  --service-account-id "$SA" \
  --parent-id "$PROJECT" \
  --output /tmp/probe.json \
  --output-format service-account-json --debug 2>&1 \
  | grep -E "grpc.service|grpc.code|resource ID"
```

If this run ever succeeds, the activation can move from the manual flow
above into `bootstrap.sh` itself.
