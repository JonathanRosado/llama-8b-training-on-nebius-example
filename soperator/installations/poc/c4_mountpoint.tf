data "nebius_iam_v1_group" "c4_dataset_viewers" {
  name      = "viewers"
  parent_id = var.iam_tenant_id
}

data "nebius_iam_v1_group" "c4_dataset_editors" {
  name      = "editors"
  parent_id = var.iam_tenant_id
}

# WHY: keep the training dataset in a dedicated bucket instead of reusing the
# Terraform state bucket. State and data have different lifecycle, blast radius,
# and access patterns; separating them makes the interview story cleaner and
# avoids accidental cross-use of highly privileged state credentials.
resource "nebius_storage_v1_bucket" "c4_datasets" {
  parent_id         = var.iam_project_id
  name              = "nebius-c4-datasets"
  versioning_policy = "DISABLED"
}

# WHY: staging and training have different trust boundaries. The write identity
# is for one-off dataset materialization; the read identity is what lands in the
# cluster-wide aws-secret consumed by the CSI driver.
resource "nebius_iam_v1_service_account" "c4_staging_write" {
  parent_id = var.iam_project_id
  name      = "nebius-c4-staging-write"
}

resource "nebius_iam_v1_service_account" "c4_reader_read" {
  parent_id = var.iam_project_id
  name      = "nebius-c4-reader-read"
}

resource "nebius_iam_v1_group_membership" "c4_staging_write_editors" {
  parent_id = data.nebius_iam_v1_group.c4_dataset_editors.id
  member_id = nebius_iam_v1_service_account.c4_staging_write.id
}

resource "nebius_iam_v1_group_membership" "c4_reader_read_viewers" {
  parent_id = data.nebius_iam_v1_group.c4_dataset_viewers.id
  member_id = nebius_iam_v1_service_account.c4_reader_read.id
}

resource "nebius_iam_v2_access_key" "c4_staging_write" {
  parent_id   = var.iam_project_id
  name        = "c4-staging-write"
  description = "Write-scoped static key for staging C4 Parquet shards into Object Storage."
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.c4_staging_write.id
    }
  }
}

resource "nebius_iam_v2_access_key" "c4_reader_read" {
  parent_id   = var.iam_project_id
  name        = "c4-reader-read"
  description = "Read-scoped static key for Mountpoint-S3 CSI access to the C4 dataset bucket."
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.c4_reader_read.id
    }
  }
}

# WHY: the Nebius Mountpoint-S3 guide calls for a kube-system Secret named
# aws-secret with literal keys key_id/access_key. Keeping the shape identical to
# the docs removes one variable when troubleshooting CSI mounts.
resource "kubernetes_secret_v1" "c4_mountpoint_reader" {
  depends_on = [module.k8s]

  metadata {
    name      = "aws-secret"
    namespace = "kube-system"
  }

  data = {
    key_id     = nebius_iam_v2_access_key.c4_reader_read.status.aws_access_key_id
    access_key = nebius_iam_v2_access_key.c4_reader_read.status.secret
  }

  type = "Opaque"
}

# WHY: Nebius Object Storage points to the upstream AWS Mountpoint-S3 CSI chart.
# Installing it in Terraform keeps Path B self-contained in the live PoC root
# rather than introducing a second GitOps path before the repo source is wired.
resource "helm_release" "aws_mountpoint_s3_csi_driver" {
  depends_on = [module.k8s]

  name             = "aws-mountpoint-s3-csi-driver"
  repository       = "https://awslabs.github.io/mountpoint-s3-csi-driver"
  chart            = "aws-mountpoint-s3-csi-driver"
  namespace        = "kube-system"
  create_namespace = false
  atomic           = true
  timeout          = 600
}

# WHY: static provisioning is the Nebius-documented path. The "storage" field is
# required by Kubernetes but ignored by the driver, so we keep the spec explicit
# and lean on mountOptions for endpoint and throughput tuning.
resource "kubernetes_persistent_volume_v1" "c4_datasets" {
  depends_on = [helm_release.aws_mountpoint_s3_csi_driver]

  metadata {
    name = "c4-datasets"
    annotations = {
      "nebius.ai/object-storage-bucket" = nebius_storage_v1_bucket.c4_datasets.name
    }
  }

  spec {
    capacity = {
      storage = "1Ti"
    }

    access_modes                     = ["ReadOnlyMany"]
    storage_class_name               = ""
    persistent_volume_reclaim_policy = "Retain"
    mount_options = [
      "endpoint-url https://storage.eu-north2.nebius.cloud:443",
      "region eu-north2",
      "maximum-throughput-gbps 10000",
      "max-threads 64",
      "metadata-ttl indefinite",
      "allow-other",
    ]

    persistent_volume_source {
      csi {
        driver        = "s3.csi.aws.com"
        volume_handle = nebius_storage_v1_bucket.c4_datasets.name
        volume_attributes = {
          bucketName = nebius_storage_v1_bucket.c4_datasets.name
        }
      }
    }
  }
}

resource "kubernetes_persistent_volume_claim_v1" "c4_datasets" {
  depends_on = [kubernetes_persistent_volume_v1.c4_datasets]

  metadata {
    name      = "c4-datasets"
    namespace = "soperator"
  }

  spec {
    access_modes       = ["ReadOnlyMany"]
    storage_class_name = ""
    volume_name        = kubernetes_persistent_volume_v1.c4_datasets.metadata[0].name

    resources {
      requests = {
        storage = "1Ti"
      }
    }
  }
}
