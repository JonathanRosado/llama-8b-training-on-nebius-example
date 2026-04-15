# -----------------------------------------------------------------------------
# Path A Terraform versions
# WHY: This root module is a long-lived PoC deliverable, not a throwaway demo.
# We pin the Nebius provider family and declare the Kubernetes providers here so
# the deployment is reproducible and reviewable in an interview. Nebius is the
# control-plane of record; Kubernetes and Helm are only used after MK8s exists.
# -----------------------------------------------------------------------------

terraform {
  required_version = ">= 1.9"

  required_providers {
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = "~> 0.5"
    }

    kubernetes = {
      source = "hashicorp/kubernetes"
    }

    helm = {
      source = "hashicorp/helm"
    }

    null = {
      source = "hashicorp/null"
    }
  }
}
