resource "terraform_data" "kubeconfig" {
  # WHY: Mutating the default kubeconfig matches the Soperator canonical flow
  # and keeps ad-hoc demo/debug on kubectl --context=path-a frictionless.
  # WHY: Overwriting the existing path-a context is deliberate for this PoC;
  # --force makes that behavior explicit instead of implicit.
  triggers_replace = [
    nebius_mk8s_v1_cluster.this.id,
    timestamp(),
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command = join(" ", [
      "nebius", "mk8s", "cluster", "get-credentials",
      "--context-name", "path-a",
      "--external",
      "--force",
      "--id", nebius_mk8s_v1_cluster.this.id,
    ])
  }
}
