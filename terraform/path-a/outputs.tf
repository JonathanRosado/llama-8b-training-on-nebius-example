# -----------------------------------------------------------------------------
# Path A outputs
# WHY: The outputs expose the minimum operator-facing values required to verify
# the deployment and hand off day-2 commands without leaking secrets.
# -----------------------------------------------------------------------------

output "k8s_cluster_id" {
  description = "Nebius Managed Kubernetes cluster ID for Path A."
  value       = nebius_mk8s_v1_cluster.this.id
}

output "nfs_server_ip" {
  description = "Private IP address of the standalone NFS server used by Slinky mounts."
  value       = module.nfs_server.nfs_server_internal_ip
}

output "slurm_cluster_name" {
  description = "Logical Slurm cluster name configured in Slinky."
  value       = var.slurm_cluster_name
}

output "kubeconfig_command" {
  description = "CLI command to materialize a kubeconfig file for the newly created cluster."
  value       = "nebius mk8s clusters get-credentials --id ${nebius_mk8s_v1_cluster.this.id} --kubeconfig-destination ./kubeconfig"
}
