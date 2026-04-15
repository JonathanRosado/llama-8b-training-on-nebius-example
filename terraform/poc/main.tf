# terraform/poc/main.tf
#
# Intentionally empty of resources. This directory exists so `terraform init`
# downloads the pinned provider, `terraform plan` succeeds with "No changes",
# and we have a known-good scaffold to add resources to incrementally.
#
# PoC-path resources land here in later steps: VPC data lookup, shared
# filesystem, scratch disk, GPU cluster, boot disks, 2× H200 VMs.
#
# Soperator / Managed Kubernetes / Slurm target-state modules will live in
# sibling root modules (e.g. terraform/mk8s/, terraform/soperator/) so the
# PoC bring-up and the target operating model stay independently runnable.

locals {
  _scaffold_marker = "terraform-poc-scaffold"
}
