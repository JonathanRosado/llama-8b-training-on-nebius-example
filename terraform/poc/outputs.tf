output "project_id" {
  description = "Nebius project the provider is bound to."
  value       = var.project_id
}

output "tenant_id" {
  description = "Nebius tenant the project lives in."
  value       = var.tenant_id
}

output "region" {
  description = "Nebius region."
  value       = var.region
}

output "profile_name" {
  description = "Nebius CLI profile the provider uses."
  value       = var.profile_name
}
