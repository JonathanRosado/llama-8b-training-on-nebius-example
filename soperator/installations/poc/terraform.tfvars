#----------------------------------------------------------------------------------------------------------------------#
#                                              Terraform - PoC values                                                  #
#                                                                                                                      #
#  Nebius AI/ML CSA take-home: 16× H200 GPUs, 2 TiB shared FS, 2 TiB network disk                                    #
#  Adapted from the vendor-canonical soperator/installations/example/terraform.tfvars                                  #
#                                                                                                                      #
#  This file configures only Soperator/Slurm-specific values. Cloud identity                                           #
#  (tenant, project, subnet, region) comes from TF_VAR_* exported by .envrc,                                           #
#  so this file works across tenants without modification.                                                             #
#----------------------------------------------------------------------------------------------------------------------#

company_name = "nebius-poc"
# production=false skips the iam_merge_request_url requirement and relaxes
# certain Soperator safeguards designed for multi-team production clusters.
production            = false
iam_merge_request_url = ""

#----------------------------------------------------------------------------------------------------------------------#
#                                                    Infrastructure                                                    #
#----------------------------------------------------------------------------------------------------------------------#

#--- Storage ---#

# Controller state stored on network SSD disk (not filestore). Starting with
# Soperator 1.22, controller spool uses CSI-provisioned disks by default.
# Filestore remains available for backward compatibility.
controller_state_on_filestore = false

# Controller spool: 128 GiB for Slurm's slurmctld state (job queue, node
# state, accounting journals). Small because it stores metadata, not data.
filestore_controller_spool = {
  spec = {
    size_gibibytes       = 128
    block_size_kibibytes = 4
  }
}

# Jail: the shared root filesystem mounted on all Slurm nodes via virtiofs.
# 2 TiB matches the PoC capacity spec from Instructions.md. Training scripts,
# datasets, checkpoints, and the Slurm environment all live here. Nebius
# attaches this at the K8s node group level (template.filesystems), bypassing
# the CSI layer for lower overhead.
filestore_jail = {
  spec = {
    size_gibibytes       = 2048
    block_size_kibibytes = 4
  }
}

# Jail submounts: additional shared filesystems mounted inside the jail for
# large data that shouldn't share backup lifecycle with the jail. Disabled
# for this PoC — the 2 TiB jail is sufficient for Llama 8B training data
# and checkpoints. The canonical validation requires allow_empty_jail_submounts
# to be explicitly set when the list is empty.
allow_empty_jail_submounts = true
filestore_jail_submounts   = []

# Node-local disks: per-worker NRD disks for scratch or container images.
# Disabled for PoC — no Enroot/Docker container workflows that need local
# scratch. The jail provides shared storage for all training I/O.
node_local_jail_submounts = []

# Pre-created PVC-backed jail submounts: used for storage that is managed
# outside the Filestore module. The Mountpoint-S3 CSI C4 dataset mount lands
# here so every slurmd worker sees the same read-only /mnt/datasets/c4 path
# inside the jail without pretending the dataset bucket is a Nebius Filestore.
persistent_volume_claim_jail_submounts = [
  {
    name       = "c4-datasets"
    mount_path = "/mnt/datasets/c4"
    claim_name = "c4-datasets"
    read_only  = true
  }
]

node_local_image_disk = {
  enabled = false
}

# Accounting filestore: stores MariaDB data for Slurm job accounting.
# Sized at 64 GiB — small because MariaDB stores Slurm job metadata
# (completed job rows, accounting tables), not any bulk data. Fits within
# the 2.5 TiB total filestore quota alongside jail (2 TiB) + controller
# spool (128 GiB) with ample headroom.
filestore_accounting = {
  spec = {
    size_gibibytes       = 64
    block_size_kibibytes = 4
  }
}

#--- NFS ---#

# In-cluster NFS server: provides a POSIX-compliant shared mount (/home or
# similar) for user home directories and tools. Disabled for PoC — the jail
# filesystem covers all shared storage needs for training. A production
# deployment would enable this for user convenience.
# All fields must be present even when disabled to satisfy Terraform type checking.
nfs_in_k8s = {
  enabled         = false
  version         = "1.2.0"
  use_stable_repo = true
  size_gibibytes  = 0
  disk_type       = "NETWORK_SSD"
  filesystem_type = "ext4"
  threads         = 1
}

#----------------------------------------------------------------------------------------------------------------------#
#                                                         Slurm                                                        #
#----------------------------------------------------------------------------------------------------------------------#

# Soperator 3.0.2 is the current stable release. The version pins the
# FluxCD bootstrap chart and all downstream Helm releases (operator, CRDs,
# slurm-cluster, nodeconfigurator, soperatorchecks).
slurm_operator_version = "3.0.2"
slurm_operator_stable  = true

# Slurm partitions: "main" is the default partition where sbatch jobs land.
# "hidden" is required by the Soperator internals for system jobs.
# OverSubscribe=YES allows multiple jobs to share nodes when resources permit.
# MaxTime=INFINITE lets jobs run without a walltime limit (appropriate for
# training experiments where duration is unpredictable).
slurm_nodesets_partitions = [
  {
    name         = "main"
    is_all       = true
    nodeset_refs = []
    config       = "Default=YES PriorityTier=10 PreemptMode=OFF MaxTime=INFINITE State=UP OverSubscribe=YES"
  },
  {
    name         = "hidden"
    is_all       = true
    nodeset_refs = []
    config       = "Default=NO PriorityTier=10 PreemptMode=OFF Hidden=YES MaxTime=INFINITE State=UP OverSubscribe=YES"
  },
]

slurm_partition_config_type = "default"

#--- Nodes ---#

# System nodes: run Soperator controller pods, FluxCD, monitoring, and other
# K8s system workloads. cpu-d3 is Nebius's AMD Epyc Genoa platform — the only
# CPU platform available on this tenant (cpu-e2 is not provisioned).
# min_size=3 is the Soperator-enforced minimum for HA. max_size=3 because
# a 2-node GPU cluster doesn't need more system capacity.
# 8vcpu-32gb is the minimum preset that passes Soperator's sufficiency check
# for system nodes.
slurm_nodeset_system = {
  min_size = 3
  max_size = 3
  resource = {
    platform = "cpu-d3"
    preset   = "8vcpu-32gb"
  }
  boot_disk = {
    type                 = "NETWORK_SSD"
    size_gibibytes       = 192
    block_size_kibibytes = 4
  }
}

# Controller: runs slurmctld (the Slurm scheduler daemon). One node is
# sufficient — Slurm's controller HA requires a separate slurmctld standby
# which Soperator doesn't currently support in self-deployed mode.
# 16vcpu-64gb matches the canonical example and is the recommended minimum
# for the controller's memory-intensive job scheduling operations.
slurm_nodeset_controller = {
  size = 1
  resource = {
    platform = "cpu-d3"
    preset   = "16vcpu-64gb"
  }
  boot_disk = {
    type                 = "NETWORK_SSD"
    size_gibibytes       = 128
    block_size_kibibytes = 4
  }
}

# GPU workers: the actual training compute. 2 nodes × 8 H200 GPUs = 16 GPUs
# total, matching the PoC capacity from Instructions.md.
# gpu-h200-sxm: NVIDIA H200 SXM with NVLink4/NVSwitch (900 GB/s bidirectional
# per GPU intra-node). 8gpu-128vcpu-1600gb: full-node preset (all 8 GPUs,
# 128 vCPUs, 1600 GB RAM).
# eu-north2-a: the H200 InfiniBand fabric in eu-north2 (NDR400, 400 Gb/s per port).
# autoscaling disabled: fixed 2 nodes for the PoC (no scale-down risk).
# boot_disk 512 GiB: Soperator-enforced minimum for GPU workers to accommodate
# container images and the Slurm jail overlay.
slurm_nodeset_workers = [
  {
    name = "gpu-h200"
    size = 2
    autoscaling = {
      enabled  = false
      min_size = null
    }
    resource = {
      platform = "gpu-h200-sxm"
      preset   = "8gpu-128vcpu-1600gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 512
      block_size_kibibytes = 4
    }
    gpu_cluster = {
      infiniband_fabric = "eu-north2-a"
    }
    preemptible                    = null
    features                       = null
    create_partition               = null
    ephemeral_nodes                = false
    initial_number_ephemeral_nodes = 1
  },
]

# Use GPU drivers pre-installed in the node OS image rather than installing
# via NVIDIA GPU Operator at runtime. Faster node startup, no driver
# download during provisioning.
use_preinstalled_gpu_drivers = true

# Login node: SSH entry point for submitting sbatch jobs. 1 node is sufficient
# for a single-user PoC. 16vcpu-64gb is the minimum preset that passes
# Soperator's sufficiency check for login nodes (needs enough memory for
# concurrent SSH sessions and user tooling).
slurm_nodeset_login = {
  size = 1
  resource = {
    platform = "cpu-d3"
    preset   = "16vcpu-64gb"
  }
  boot_disk = {
    type                 = "NETWORK_SSD"
    size_gibibytes       = 256
    block_size_kibibytes = 4
  }
}

# Accounting nodeset definition required by the canonical validation even when
# accounting_enabled=false. The Terraform variable validation accesses
# .boot_disk.size_gibibytes unconditionally, so we provide a minimal definition.
# No actual node is created when accounting_enabled=false.
slurm_nodeset_accounting = {
  resource = {
    platform = "cpu-d3"
    preset   = "8vcpu-32gb"
  }
  boot_disk = {
    type                 = "NETWORK_SSD"
    size_gibibytes       = 128
    block_size_kibibytes = 4
  }
}

# NFS nodeset: runs the in-cluster NFS server pod. Null because nfs_in_k8s
# is disabled above.
slurm_nodeset_nfs = null

#--- Login ---#

# Public IP on the login load balancer so we can SSH from outside the VPC.
# Acceptable for a PoC; production would use Tailscale or a bastion.
slurm_login_public_ip = true
# Tailscale VPN disabled — direct SSH access is simpler for a PoC demo.
tailscale_enabled = false
# SSSD (System Security Services Daemon) disabled — no LDAP/AD integration
# needed for a single-user PoC. Production multi-tenant clusters would
# enable this for centralized user management.
slurm_sssd_enabled                     = false
slurm_sssd_conf_secret_ref_name        = ""
slurm_sssd_ldap_ca_config_map_ref_name = ""

# Root SSH access to login nodes. PoC-only — production would use individual
# user accounts via SSSD/LDAP. Root is acceptable here because the PoC has a
# single operator and the cluster is ephemeral.
slurm_login_ssh_root_public_keys = [
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA2lAlEampdw9tAr8hLme1x72vvqISH5MraC+uRv4YXr rosado@jon",
]

#--- Exporter ---#

# Slurm metrics exporter feeds Prometheus with job/node/queue metrics.
# Enabled even for PoC — useful for monitoring training job efficiency.
slurm_exporter_enabled = true

#--- ActiveChecks ---#

# prod_quick: runs SSH connectivity + IB/GPU performance checks after
# provisioning. Takes ~10 minutes on H200. Catches fabric issues before
# training starts without the 30-min to 2-hour cost of prod_acceptance
# (which adds DCGM stress tests and full NCCL sweeps). Our custom
# validator script complements this with structured reporting.
active_checks_scope = "prod_quick"

#--- Config ---#

# Shared memory for Slurm controller and worker containers. 64 GiB is
# sufficient for the 8B model. The canonical example uses 1024 GiB for
# 128-node H100 clusters running 70B+ models. Shared memory backs
# /dev/shm inside containers, used by PyTorch DataLoader workers and
# NCCL for intra-node shared memory transport.
slurm_shared_memory_size_gibibytes = 64

# Exclude the controller from Soperator's maintenance controller — let the
# MK8s control plane handle its lifecycle instead. Workers and login nodes
# remain under Soperator maintenance management.
maintenance_ignore_node_groups = ["controller"]

#--- Telemetry ---#

# Telemetry sends cluster health metrics to Nebius's observability backend.
# DCGM job mapping adds hpc_job labels to GPU metrics so per-job GPU
# utilization is visible in dashboards.
telemetry_enabled        = true
dcgm_job_mapping_enabled = true
# Public O11y disabled — the PoC doesn't need externally accessible dashboards.
public_o11y_enabled = false
soperator_notifier  = { enabled = false }

#--- Accounting ---#

# Slurm accounting MUST be enabled in Soperator 3.0.2 — the operator's
# rest reconciliation at internal/controller/clustercontroller/rest.go:37
# short-circuits when accounting is disabled:
#   if !isAccountingEnabled && !isDBEnabled { skip }
# which means no slurmrestd Deployment + no soperator-rest-svc Service,
# which in turn blocks the ActiveCheck controller (it needs the REST API
# to list Slurm nodes) and the Helm `wait-for-active-checks` pre-install
# hook loops forever. Enabling accounting costs one accounting CPU node
# (cpu-d3 8vcpu-32gb) + a 64 GiB filestore for MariaDB — small overhead
# for the canonical CSA-defensible path. Production clusters want this
# anyway for job history, fairshare scheduling, and cost allocation.
accounting_enabled = true

#--- Backups ---#

# Jail backups disabled for PoC. The canonical path uses K8up + restic to
# back up the jail filesystem to an S3 bucket on a schedule. Disabled here
# because (a) the PoC data is ephemeral and (b) backup infrastructure adds
# setup time. Production deployments should set to "auto" (backs up jails
# under 12 TiB automatically).
backups_enabled           = "force_disable"
backups_password          = ""
backups_schedule          = "@daily-random"
backups_prune_schedule    = "@daily-random"
backups_retention         = { keepDaily = 7 }
cleanup_bucket_on_destroy = false

#--- Kubernetes ---#

# K8s 1.32: required by Soperator's GPU driver presets for H200. The
# driver_presets.tf validation only supports 1.32 and 1.33 with
# preinstalled GPU images. 1.31 fails the validation check.
k8s_version = 1.32

# NVIDIA kernel module options written to /etc/modprobe.d/nvidia_config.conf
# via cloud-init on GPU worker nodes:
# - RestrictProfilingToAdminUsers=0: allows non-root users to use nsys/ncu
#   profilers — essential for ML engineers profiling training performance
# - EnableStreamMemOPs=1: enables CUDA stream-ordered memory operations
#   for better memory allocation performance in training frameworks
# - PeerMappingOverride=1: enables GPU peer-to-peer memory mapping even
#   when the kernel would otherwise disable it — required for NVLink
#   direct access between GPUs in some driver configurations
nvidia_config_lines = [
  "options nvidia NVreg_RestrictProfilingToAdminUsers=0",
  "options nvidia NVreg_EnableStreamMemOPs=1",
  "options nvidia NVreg_RegistryDwords=\"PeerMappingOverride=1;\"",
]
