# -----------------------------------------------------------------------------
# Path A provider configuration
# WHY: Path A must follow the same short-lived, exec-backed authentication
# posture as the rest of the repo. For Nebius itself we intentionally reuse the
# local CLI profile instead of storing static tokens in Terraform. For
# Kubernetes and Helm we use the Managed Kubernetes exec-credential flow so no
# bearer token is hard-coded in configuration or state.
# -----------------------------------------------------------------------------

provider "nebius" {
  # WHY: Empty profile block defers to the Nebius CLI's currently active
  # profile (the one marked [default] in `nebius profile list`). This matches
  # the canonical Solutions Library pattern used by Path B (soperator) and
  # avoids hardcoding a profile name that only works on one operator's laptop.
  # Credentials stay in the CLI config, never in Terraform state.
  profile = {}

  # Nebius uses the EU control-plane domain for the managed services in this
  # project. Keeping it explicit avoids ambiguity during review.
  domain = "api.eu.nebius.cloud:443"
}

provider "kubernetes" {
  # Managed Kubernetes exposes the API endpoint after cluster creation. The
  # provider wiring is therefore directly anchored to the cluster resource.
  # WHY: public_endpoint already includes the scheme and port (e.g.
  # "https://pu.mk8scluster-<id>.mk8s.eu-north2.nebius.cloud:443"). Prepending
  # another "https://" yields a malformed URL that routes the k8s client to
  # host=https and path=/pu.mk8scluster-... — exactly what the first apply
  # surfaced. Path B uses the attribute verbatim for the same reason.
  host                   = nebius_mk8s_v1_cluster.this.status.control_plane.endpoints.public_endpoint
  cluster_ca_certificate = nebius_mk8s_v1_cluster.this.status.control_plane.auth.cluster_ca_certificate

  # WHY: `nebius mk8s v1 cluster get-token --format json` returns an
  # ExecCredential-shaped token for the currently active CLI profile. This is
  # the same command Path B wires through its Soperator module, and it is the
  # only mk8s subcommand that emits kubectl's exec-plugin JSON. The sibling
  # `get-credentials` writes a kubeconfig file to disk instead of stdout, which
  # is why the first apply failed with "exit code 2". No --id flag is needed
  # because the token is account-scoped, not cluster-scoped.
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "nebius"
    args = [
      "mk8s",
      "v1",
      "cluster",
      "get-token",
      "--format",
      "json",
    ]
  }
}

provider "helm" {
  kubernetes = {
    host                   = nebius_mk8s_v1_cluster.this.status.control_plane.endpoints.public_endpoint
    cluster_ca_certificate = nebius_mk8s_v1_cluster.this.status.control_plane.auth.cluster_ca_certificate
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "nebius"
      args = [
        "mk8s",
        "v1",
        "cluster",
        "get-token",
        "--format",
        "json",
      ]
    }
  }
}
