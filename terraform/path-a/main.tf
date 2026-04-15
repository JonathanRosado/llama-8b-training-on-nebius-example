# -----------------------------------------------------------------------------
# Path A core infrastructure
# WHY: This file owns the durable cloud primitives for Path A: one MK8s control
# plane, one system node group for cluster services, one GPU node group for H200
# workers, and an optional Nebius GPU cluster to provide InfiniBand. This keeps
# the separation of concerns clean: Terraform handles substrate creation, Helm
# layers on operators, and Slinky turns the GPU nodes into Slurm workers.
# -----------------------------------------------------------------------------

data "nebius_vpc_v1_subnet" "selected" {
  # WHY: We look up the existing subnet rather than trusting a raw string so the
  # config can validate network metadata and reuse the subnet's actual CIDR in
  # the NFS export policy.
  id = var.subnet_id
}

locals {
  # WHY: Test mode is allowed to trade HA for cost in the control-plane backing
  # etcd. In production the defensible default is 3 members for quorum safety.
  etcd_cluster_size = var.test_mode ? 1 : 3

  # WHY: The system pool is intentionally modest. It only needs to run core
  # services, cert-manager, the GPU/network operators, and Slinky control pods.
  system_node_count = 2

  # WHY: These labels are the contract between MK8s scheduling and the Slinky
  # Helm values. We keep the vocabulary explicit so operators can reason about
  # placement without reverse engineering selectors.
  common_node_group_labels = {
    "library-solution" = "k8s-training"
    "path"             = "a"
  }
}

resource "nebius_mk8s_v1_cluster" "this" {
  parent_id = var.parent_id
  name      = "${var.slurm_cluster_name}-k8s"

  control_plane = {
    # WHY: The cluster subnet is an explicit input because Path A must land in
    # the approved eu-north2 subnet rather than creating ad hoc networking.
    subnet_id = data.nebius_vpc_v1_subnet.selected.id

    # WHY: H200 compatibility and the Soperator/Slinky version choices in this
    # interview converged on Kubernetes 1.32.
    version = var.k8s_version

    endpoints = {
      # WHY: Public API access keeps the PoC operable from an engineer laptop and
      # is consistent with the repo's existing MK8s bootstrap pattern.
      public_endpoint = {}
    }

    etcd_cluster_size = local.etcd_cluster_size
  }
}

# WHY (no gpu_cluster resource): Path A represents the tenant's day-zero state
# where capacity reservations / gpu_cluster quota have NOT been approved. The
# 8 Mellanox HCAs per H200 node are still physically present but are not
# grouped into an InfiniBand fabric, so NCCL falls back to sockets over the
# 200 Gbps Ethernet interface. The IB-equipped variant lives on Path B
# (soperator/installations/poc/) and on git tag `path-a-ib-working-v1` for
# reference. This keeps the dual-path narrative honest: Path A = "what works
# with zero approvals", Path B = "what the customer gets once reservations land".

resource "nebius_mk8s_v1_node_group" "system" {
  parent_id        = nebius_mk8s_v1_cluster.this.id
  name             = "${var.slurm_cluster_name}-system"
  fixed_node_count = local.system_node_count
  version          = var.k8s_version
  labels = merge(local.common_node_group_labels, {
    "role" = "system"
  })

  template = {
    metadata = {
      labels = {
        "role" = "system"
      }
    }

    # WHY: 128 GiB is a conservative baseline for kube-system, operators, image
    # pulls, and log growth without burning storage budget on non-worker nodes.
    boot_disk = {
      size_gibibytes = 128
      type           = "NETWORK_SSD"
    }

    network_interfaces = [
      {
        subnet_id = data.nebius_vpc_v1_subnet.selected.id
      }
    ]

    resources = {
      platform = var.system_node_platform
      preset   = var.system_node_preset
    }
  }
}

resource "nebius_mk8s_v1_node_group" "gpu" {
  parent_id        = nebius_mk8s_v1_cluster.this.id
  name             = "${var.slurm_cluster_name}-gpu"
  fixed_node_count = var.gpu_nodes_count
  version          = var.k8s_version
  labels = merge(local.common_node_group_labels, {
    "role" = "gpu-worker"
  })

  template = {
    metadata = {
      labels = {
        "role" = "gpu-worker"
      }
    }

    # WHY: GPU nodes need larger ephemeral capacity for container layers, job
    # artifacts, and NCCL scratch space even though durable state lives on NFS/S3.
    boot_disk = {
      size_gibibytes = 512
      type           = "NETWORK_SSD"
    }

    network_interfaces = [
      {
        subnet_id = data.nebius_vpc_v1_subnet.selected.id
      }
    ]

    resources = {
      platform = var.gpu_platform
      preset   = var.gpu_preset
    }

    # WHY (explicit gpu_cluster = null): Nebius's API rejects deleting a
    # gpu_cluster while instances are still attached to it. Omitting the
    # attribute entirely isn't enough — the provider skips sending a node-
    # group update at all. Setting `gpu_cluster = null` explicitly makes
    # Terraform send a patch that detaches the node group from the fabric
    # before the gpu_cluster resource gets deleted. Path B binds an actual
    # cluster via the Soperator root; compare soperator/installations/poc/main.tf.
    gpu_cluster = null
  }
}
