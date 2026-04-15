# -----------------------------------------------------------------------------
# Path A NFS storage
# WHY: Path A deliberately uses a simple NFS-over-disk design instead of
# Filestore so the PoC stays explainable and cost-controlled. This is a single
# point of failure, which is acceptable for the demo because checkpoint-worthy
# workloads are expected to externalize durable artifacts to S3. The NFS export
# only carries shared home/state/scratch semantics for the Slurm control plane.
# -----------------------------------------------------------------------------

locals {
  # WHY: The subnet data source gives us the actual private CIDR used by the
  # selected subnet, which is safer than hard-coding a guessed export range.
  nfs_export_cidr = data.nebius_vpc_v1_subnet.selected.status.ipv4_private_cidrs[0]

  # WHY: The NFS module does not expose first-class subdirectory creation, so we
  # bootstrap it over SSH after provisioning. Only /nfs/home and /nfs/shared
  # are exported: slurmctld state lives on a local RWO PVC inside the cluster
  # (see controller.persistence in slinky_values.yaml.tftpl) because upstream
  # Slurm and Slinky both flag NFS as the wrong medium for StateSaveLocation.
  nfs_required_directories = [
    "/nfs/home",
    "/nfs/shared",
  ]

  # WHY: The module also does not expose an SSH bootstrap abstraction. We use a
  # conventional local key lookup so the root remains self-contained without
  # modifying the shared module.
  bootstrap_public_key_candidates = [
    pathexpand("~/.ssh/id_ed25519.pub"),
    pathexpand("~/.ssh/id_rsa.pub"),
  ]
  bootstrap_private_key_candidates = [
    pathexpand("~/.ssh/id_ed25519"),
    pathexpand("~/.ssh/id_rsa"),
  ]
  bootstrap_public_key_path = try(one([
    for path in local.bootstrap_public_key_candidates : path if fileexists(path)
  ]), null)
  bootstrap_private_key_path = try(one([
    for path in local.bootstrap_private_key_candidates : path if fileexists(path)
  ]), null)
}

module "nfs_server" {
  source = "../../modules/nfs-server"

  parent_id = var.parent_id
  subnet_id = data.nebius_vpc_v1_subnet.selected.id

  # WHY: The module is subnet-scoped, not zone-scoped; in Nebius the subnet
  # already carries placement in the approved availability zone.
  nfs_ip_range = local.nfs_export_cidr

  # WHY: The shared module expects bytes for capacity. We keep the operator input
  # in GiB and convert here so the root stays readable.
  nfs_size = var.nfs_disk_size_gb * 1024 * 1024 * 1024

  disk_type            = var.nfs_disk_type
  nfs_disk_name_suffix = "path-a"
  instance_name        = "${var.slurm_cluster_name}-nfs"

  # WHY: This public IP exists solely because the shared module cannot yet create
  # the required NFS subdirectories in cloud-init. In production we would remove
  # the public exposure and move this bootstrap into image/cloud-init/bastion flow.
  public_ip = true

  ssh_public_keys = local.bootstrap_public_key_path != null ? [
    trimspace(file(local.bootstrap_public_key_path))
  ] : []
}

resource "null_resource" "nfs_subdirectories" {
  depends_on = [
    module.nfs_server,
  ]

  triggers = {
    nfs_server_ip = module.nfs_server.nfs_server_public_ip
    directories   = join(",", local.nfs_required_directories)
  }

  lifecycle {
    precondition {
      condition = (
        local.bootstrap_public_key_path != null &&
        local.bootstrap_private_key_path != null
      )
      error_message = "NFS bootstrap requires a local SSH key pair at ~/.ssh/id_ed25519(.pub) or ~/.ssh/id_rsa(.pub)."
    }
  }

  connection {
    type        = "ssh"
    host        = module.nfs_server.nfs_server_public_ip
    user        = "nfs"
    private_key = file(local.bootstrap_private_key_path)
    timeout     = "10m"
  }

  # WHY: Slurm mounts subdirectories rather than the export root so that home
  # and shared scratch remain explicitly separated even on one server.
  #
  # WHY (cloud-init wait + mountpoint check): The NFS server's cloud-init
  # assembles a RAID 0, mkfs's /dev/md0, and mounts it on /nfs after SSH is
  # already accepting connections. Without an explicit wait, this provisioner
  # races the mount and ends up creating subdirectories on the root filesystem
  # which then get shadowed when /dev/md0 mounts on top. The symptom is an
  # apparently-healthy server (showmount -e works) where pod mounts fail with
  # 'access denied' because the subdirectory paths disappear under the RAID
  # mount. Waiting on cloud-init + asserting /nfs is a mountpoint makes the
  # bootstrap deterministic.
  provisioner "remote-exec" {
    inline = [
      "sudo cloud-init status --wait >/dev/null 2>&1 || true",
      "for i in $(seq 1 60); do mountpoint -q /nfs && break; echo 'waiting for /nfs RAID mount'; sleep 2; done",
      "mountpoint -q /nfs || (echo 'ERROR: /nfs never became a mountpoint' >&2; exit 1)",
      "sudo mkdir -p /nfs/home /nfs/shared",
      "sudo chmod 0777 /nfs/home /nfs/shared",
      "sudo chown nobody:nogroup /nfs/home /nfs/shared",
      # WHY (|| true): the upstream nfs-server module's cloud-init template
      # writes the export line to /etc/exports twice (once in write_files,
      # again in runcmd >> /etc/exports), so exportfs -ra reports "duplicated
      # export entries" and exits 1 even though the kernel table is updated
      # correctly. Tolerate the non-zero exit — the reload itself is a belt-
      # and-braces hygiene step after mkdir, not strictly required since
      # subdirectories inherit from the parent export.
      "sudo exportfs -ra || true",
    ]
  }
}
