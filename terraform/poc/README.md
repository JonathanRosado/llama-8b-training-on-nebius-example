# terraform/poc/

PoC Terraform root module for the Nebius take-home.

Currently a **scaffold** — no resources are declared. The goal of this
step is only: "Terraform can talk to Nebius under the dedicated service
account, state is initialized, a plan runs cleanly." Real resources
(GPU cluster, shared FS, scratch disk, boot disks, VMs) will be added in
subsequent commits.

## Prerequisites

Run the identity bootstrap once:

```bash
bash ../bootstrap/bootstrap.sh
```

This pre-stages the automation-lane service account. Day-1 Terraform
work uses the human lane (`rosadoft` federated profile), which is
Nebius's documented interactive pattern and what the default
`profile_name` resolves to in `poc.auto.tfvars`.

When the automation lane is activated later (see
`../bootstrap/IDENTITY.md` → *Activating the automation lane*),
re-running `bootstrap.sh` wires up a `nebius-terraform` CLI profile.
At that point switch `poc.auto.tfvars`:

```bash
sed -i 's/profile_name = "rosadoft"/profile_name = "nebius-terraform"/' \
    poc.auto.tfvars
terraform init -reconfigure
terraform plan
```

No state migration is needed — only the provider's auth target changes.

## Running it

```bash
cd terraform/poc
terraform init     # downloads pinned Nebius provider
terraform plan     # should report "No changes"
```

The concrete values used by this directory live in `poc.auto.tfvars`
(gitignored). An example is at `poc.auto.tfvars.example`.

## Files

| File | Purpose |
|---|---|
| `versions.tf` | Pin Terraform core and the Nebius provider. |
| `providers.tf` | `provider "nebius"` block that reads auth from the CLI profile. |
| `variables.tf` | Typed input variables. |
| `main.tf` | Resources (empty for now). |
| `outputs.tf` | Echoes project/tenant/region/profile for downstream scripts. |
| `poc.auto.tfvars.example` | Checked-in example values. |
| `poc.auto.tfvars` | Real values (gitignored). |

## State

Local backend only for now — `terraform.tfstate` lives in this directory
and is gitignored. This is a deliberate Day-1 choice, not a gap I missed:

- The remote-state target of record would be a Nebius Object Storage
  bucket using the S3-compatible backend. We don't have an Object Storage
  bucket yet, and bootstrapping one creates its own chicken-and-egg
  question (the bucket itself wants to be in Terraform state).
- Local state is acceptable as long as the operator is a single
  workstation — the PoC's working assumption.
- State file contents include tenant/project IDs and any output values;
  no secrets are written into state by the current scaffold because
  there are no resources yet.
- The migration path is mechanical once the bucket exists: add a
  `backend "s3"` block pointing at the bucket, run
  `terraform init -migrate-state`, and commit the new `backend.tf`.

If an interviewer asks "why no remote backend yet", that's the answer.
