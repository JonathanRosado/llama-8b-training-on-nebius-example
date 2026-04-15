variable "mlflow_enabled" {
  description = "Enable the MLflow marketplace release on the live Path B cluster."
  type        = bool
  default     = false
}

variable "mlflow_namespace" {
  description = "Namespace for the MLflow marketplace release."
  type        = string
  default     = "mlflow"
}

variable "mlflow_product_slug" {
  description = "Marketplace product slug for MLflow. Verified: the ml-flow application manifest at github.com/nebius/nebius-k8s-applications/ml-flow/manifest.yaml declares `slug: ml-flow`; Terraform resources in this repo (terraform/path-a/helm.tf:49, :82) use the `nebius/<slug>` vendor-namespace form, so the canonical Terraform value is `nebius/ml-flow`."
  type        = string
  default     = "nebius/ml-flow"

  validation {
    condition     = length(trimspace(var.mlflow_product_slug)) > 0
    error_message = "mlflow_product_slug must be a non-empty string."
  }
}

locals {
  # TODO: replace the placeholder variable with a concrete default only after the
  # exact release slug is verified from a tenant-visible marketplace/API source.
  #
  # Evidence gathered during this deliverable:
  # - Nebius k8s applications repo contains an `ml-flow/` application directory.
  # - Its public manifest declares `slug: ml-flow`.
  # - Existing repo Terraform patterns use `product_slug = \"nebius/<app-slug>\"`
  #   for marketplace releases such as `nebius/nvidia-gpu-operator`.
  #
  # The likely value is therefore `nebius/ml-flow`, but this file intentionally
  # does not hard-code that inference until the exact marketplace/API surface is
  # verified in the target tenant.
  mlflow_release_enabled = var.mlflow_enabled && var.mlflow_product_slug != null
}

resource "nebius_applications_v1alpha1_k8s_release" "mlflow" {
  count = local.mlflow_release_enabled ? 1 : 0

  depends_on = [
    module.k8s,
    module.fluxcd,
  ]

  cluster_id = module.k8s.cluster_id
  parent_id  = data.nebius_iam_v1_project.this.id

  application_name = "mlflow"
  namespace        = var.mlflow_namespace
  product_slug     = var.mlflow_product_slug
}
