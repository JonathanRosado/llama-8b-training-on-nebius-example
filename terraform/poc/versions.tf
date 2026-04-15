terraform {
  required_version = ">= 1.6.0"

  required_providers {
    nebius = {
      # Nebius publishes its provider on a tenant-agnostic registry hosted
      # on Nebius Object Storage, not on registry.terraform.io.
      # https://docs.nebius.com/terraform-provider/quickstart
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = ">= 0.5.55"
    }
  }
}
