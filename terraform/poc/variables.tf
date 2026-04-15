variable "profile_name" {
  description = <<-EOT
    Nebius CLI profile this provider authenticates through.

    Defaults to the human-lane federated profile (`rosadoft`) for
    interactive Day-1 Terraform work. Switch to the automation-lane
    profile (`nebius-terraform`) once the automation lane is activated —
    see terraform/bootstrap/IDENTITY.md.
  EOT
  type        = string
  default     = "rosadoft"
}

variable "region" {
  description = "Nebius region. This project is bound to eu-north1."
  type        = string
  default     = "eu-north1"
}

variable "project_id" {
  description = "Nebius project (parent) id. Typically captured via `nebius config get parent-id`."
  type        = string
}

variable "tenant_id" {
  description = "Nebius tenant id. Typically captured via `nebius config get tenant-id`."
  type        = string
}
