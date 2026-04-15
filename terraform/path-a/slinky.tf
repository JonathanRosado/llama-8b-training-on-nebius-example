# -----------------------------------------------------------------------------
# Path A Slinky installation
# WHY: Path A uses SchedMD's official Slurm-on-Kubernetes stack. The CRDs,
# operator, and Slurm chart are pinned to v1.1.0 — the current stable release
# published on 2026-04-02 — as a lockstep unit so that CRDs, operator, and
# chart templates never drift against each other. The 0.x series (which
# capped at 0.4.1) is explicitly avoided because 1.0 is the first release the
# Slinky team calls production-stable.
# -----------------------------------------------------------------------------

resource "helm_release" "slurm_operator_crds" {
  depends_on = [
    nebius_mk8s_v1_node_group.gpu,
    helm_release.cert_manager_platform,
  ]

  name             = "slurm-operator-crds"
  namespace        = "slinky-system"
  create_namespace = true

  # WHY: The vendor-canonical OCI location documented by SchedMD uses the
  # slinkyproject chart namespace.
  repository = "oci://ghcr.io/slinkyproject/charts"
  chart      = "slurm-operator-crds"
  version    = "1.1.0"
}

resource "helm_release" "slurm_operator" {
  depends_on = [
    nebius_mk8s_v1_node_group.gpu,
    helm_release.cert_manager_platform,
    helm_release.slurm_operator_crds,
    nebius_applications_v1alpha1_k8s_release.gpu_operator_platform,
  ]

  name             = "slurm-operator"
  namespace        = "slinky-system"
  create_namespace = true

  repository = "oci://ghcr.io/slinkyproject/charts"
  chart      = "slurm-operator"
  version    = "1.1.0"
}

resource "terraform_data" "wait_for_gpu_capacity" {
  depends_on = [
    terraform_data.kubeconfig,
    nebius_mk8s_v1_node_group.gpu,
    nebius_applications_v1alpha1_k8s_release.gpu_operator_platform,
    helm_release.slurm_operator,
  ]

  input = {
    gpu_nodes_count = var.gpu_nodes_count
  }

  # WHY: The Nebius marketplace GPU operator can report DEPLOYED before the
  # device plugin finishes advertising nvidia.com/gpu capacity on every GPU
  # node. Gate the Slurm chart on the actual kubelet capacity signal so slurmd
  # does not register a transient 0-GPU inventory and get parked in INVALID_REG.
  # WHY: local-exec + kubectl is intentional here because the Kubernetes
  # provider cannot block-until-condition; polling is required either way, and
  # local-exec + kubectl is the idiomatic Nebius Solutions Library pattern
  # (see soperator/modules/login/main.tf's wait_for_slurm_login_service).
  # WHY: Only this resource declares terraform_data.kubeconfig in depends_on
  # because it is the only kubectl-based local-exec in terraform/path-a/ today.
  # Future additions (e.g. post-apply validator bootstrap, slurmd sanity-check
  # job submission) should declare the same dependency rather than re-deriving
  # a kubeconfig, so the named context path-a is the single access primitive.
  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # WHY (jsonpath dot-escape): kubectl's jsonpath implementation does not
      # support bracket-with-quotes for string keys — {.status.capacity['nvidia.com/gpu']}
      # silently resolves to empty. Only the dot-notation form with backslash-
      # escaped dots inside the key works: {.status.capacity.nvidia\.com/gpu}.
      # The \\ below becomes a literal \ after HCL escape processing, then
      # bash's single quotes preserve it, so kubectl finally sees \. which its
      # jsonpath parser treats as a literal dot in the key name.
      deadline=$((SECONDS + 900))
      while [ "$SECONDS" -lt "$deadline" ]; do
        mapfile -t nodes < <(kubectl --context=path-a get nodes -l role=gpu-worker -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

        if [ "$${#nodes[@]}" -ne "${self.input.gpu_nodes_count}" ]; then
          sleep 10
          continue
        fi

        all_ready=1
        for node in "$${nodes[@]}"; do
          gpu_count=$(kubectl --context=path-a get "node/$node" -o jsonpath='{.status.capacity.nvidia\.com/gpu}' 2>/dev/null)
          if [ "$gpu_count" != "8" ]; then
            all_ready=0
            break
          fi
        done

        if [ "$all_ready" -eq 1 ]; then
          exit 0
        fi

        sleep 10
      done

      echo "Timed out after 15m waiting for nvidia.com/gpu=8 on all ${self.input.gpu_nodes_count} GPU nodes" >&2
      exit 1
    EOT
  }
}

resource "helm_release" "slurm" {
  depends_on = [
    nebius_mk8s_v1_node_group.gpu,
    helm_release.slurm_operator,
    terraform_data.wait_for_gpu_capacity,
    null_resource.nfs_subdirectories,
  ]

  name             = "slurm"
  namespace        = "default"
  create_namespace = true

  repository = "oci://ghcr.io/slinkyproject/charts"
  chart      = "slurm"
  version    = "1.1.0"

  values = [
    templatefile("${path.module}/slinky_values.yaml.tftpl", {
      nfs_server_ip      = module.nfs_server.nfs_server_internal_ip
      slurm_cluster_name = var.slurm_cluster_name
      gpu_nodes_count    = var.gpu_nodes_count
    })
  ]
}
