# -----------------------------------------------------------------------------
# Path A platform operators
# WHY: These releases install the minimum control-plane software the MK8s
# substrate needs before Slurm is layered on top: cert-manager for webhook/TLS
# dependencies and NVIDIA GPU Operator with drivers disabled because Nebius
# already provisions the node-side drivers for managed GPU nodes. The Nebius
# network operator is installed through the Nebius marketplace module because
# that is the provider-canonical path already used elsewhere in the repo.
# -----------------------------------------------------------------------------

resource "helm_release" "cert_manager_platform" {
  depends_on = [
    nebius_mk8s_v1_node_group.system,
  ]

  name             = "cert-manager"
  namespace        = "cert-manager"
  create_namespace = true

  repository = "https://charts.jetstack.io"
  chart      = "cert-manager"
  version    = "1.15.3"

  # WHY: Slinky admission webhooks expect cert-manager to manage certificates.
  values = [
    yamlencode({
      crds = {
        enabled = true
      }
    })
  ]
}

resource "nebius_applications_v1alpha1_k8s_release" "network_operator_platform" {
  depends_on = [
    nebius_mk8s_v1_node_group.system,
    nebius_mk8s_v1_node_group.gpu,
  ]

  # WHY: Nebius ships the network operator as a marketplace application. Using
  # the first-party release resource keeps Path A on the vendor-canonical
  # install path for InfiniBand networking instead of guessing at an external
  # Helm repository.
  cluster_id = nebius_mk8s_v1_cluster.this.id
  parent_id  = var.parent_id

  application_name = "network-operator"
  namespace        = "nebius-system"
  product_slug     = "nebius/nvidia-network-operator"

  sensitive = {
    set = {
      "operator.resources.limits.cpu"             = "500m"
      "operator.resources.limits.memory"          = "512Mi"
      "operator.ofedDriver.livenessProbe.enabled" = false
    }
  }
}

resource "nebius_applications_v1alpha1_k8s_release" "gpu_operator_platform" {
  depends_on = [
    nebius_mk8s_v1_node_group.system,
    nebius_mk8s_v1_node_group.gpu,
    nebius_applications_v1alpha1_k8s_release.network_operator_platform,
  ]

  # WHY: Install the GPU operator through the Nebius marketplace release
  # (product_slug "nebius/nvidia-gpu-operator") instead of the upstream NVIDIA
  # Helm chart. The marketplace build is pre-configured for the Nebius managed
  # node image's driver paths, runtime class, and node labels — exactly the
  # wiring the Solutions Library k8s-training module uses at
  # modules/gpu-operator/main.tf. The previous upstream chart with
  # driver.enabled=false wedged on the driver-validation initContainer because
  # it assumed standard NVIDIA driver paths that Nebius provisions elsewhere,
  # which in turn blocked every downstream DaemonSet with
  # 'no runtime for "nvidia" is configured'.
  cluster_id = nebius_mk8s_v1_cluster.this.id
  parent_id  = var.parent_id

  application_name = "gpu-operator"
  namespace        = "gpu-operator"
  product_slug     = "nebius/nvidia-gpu-operator"
}
