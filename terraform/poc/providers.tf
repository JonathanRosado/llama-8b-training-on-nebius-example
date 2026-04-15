provider "nebius" {
  # Auth is reused from the local Nebius CLI profile. The provider's
  # `profile` argument is an object (not a bare string) per
  # https://docs.nebius.com/terraform-provider/reference/provider.
  # The CLI profile file is ~/.nebius/config.yaml by default.
  profile = {
    name = var.profile_name
  }
}
