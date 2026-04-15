# -----------------------------------------------------------------------------
# Path A input variables
# WHY: The variable surface is intentionally small and interview-defensible. The
# inputs capture customer-specific placement and sizing choices while the module
# retains enough defaults to stay reproducible in the hiring sandbox.
# -----------------------------------------------------------------------------

variable "parent_id" {
  description = "Nebius project ID that owns the Path A resources."
  type        = string
}

variable "subnet_id" {
  description = "Existing Nebius VPC subnet ID used for both MK8s nodes and the NFS server."
  type        = string
}

variable "region" {
  description = "Nebius region for the deployment; Path A is approved for eu-north2."
  type        = string
  default     = "eu-north2"
}

variable "k8s_version" {
  description = "Managed Kubernetes version. Path A pins 1.32 because the H200/Soperator compatibility work requires it."
  type        = string
  default     = "1.32"
}

# WHY (no enable_gpu_cluster, enable_filestore, availability_zone): Path A is
# intentionally the zero-approvals baseline. Enabling either gpu_cluster (IB
# fabric) or filestore (Nebius managed filesystem) requires quota that the
# customer does not have on day zero. Both variables would always be false on
# this path, so removing them forces the "constrained" choice to be structural
# rather than a toggle an operator might flip by mistake. The IB-equipped
# variant lives on Path B; see git tag path-a-ib-working-v1 for the previous
# IB-enabled Path A configuration.

variable "test_mode" {
  description = "Whether to use cost-aware demo sizing for components that can be safely reduced in a PoC."
  type        = bool
  default     = true
}

variable "gpu_nodes_count" {
  description = "Number of H200 worker nodes in the GPU node group."
  type        = number
  default     = 2
}

variable "gpu_platform" {
  description = "Nebius GPU platform for the worker node group."
  type        = string
  default     = "gpu-h200-sxm"
}

variable "gpu_preset" {
  description = "Nebius preset for the GPU worker nodes. The 8-GPU preset maps to a full SXM baseboard, which is also the unit of per-node allocation Slurm reasons about."
  type        = string
  default     = "8gpu-128vcpu-1600gb"
}

variable "system_node_platform" {
  description = "Nebius CPU platform for the system node group that hosts cluster services and Slinky control-plane pods."
  type        = string
  default     = "cpu-d3"
}

variable "system_node_preset" {
  description = "Nebius preset for the system node group."
  type        = string
  default     = "8vcpu-32gb"
}

variable "nfs_disk_size_gb" {
  description = "Capacity of the backing disk for the standalone NFS server, expressed in GiB for operator readability."
  type        = number
  default     = 2048
}

variable "nfs_disk_type" {
  description = "Nebius disk class for the NFS data disk. Network SSD is the conservative default for a shared metadata/data path."
  type        = string
  default     = "NETWORK_SSD"
}

variable "slurm_cluster_name" {
  description = "Logical Slurm cluster name exposed to Slinky and to end users."
  type        = string
  default     = "path-a-slurm"
}
