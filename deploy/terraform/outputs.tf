output "cluster_name" {
  value = google_container_cluster.inference.name
}

output "cluster_region" {
  value = google_container_cluster.inference.location
}

output "artifact_registry_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.containers.repository_id}"
}

output "redis_host" {
  value = google_redis_instance.rate_limits.host
}

output "redis_port" {
  value = google_redis_instance.rate_limits.port
}

output "redis_ca_cert" {
  value     = try(google_redis_instance.rate_limits.server_ca_certs[0].cert, null)
  sensitive = true
}

output "redis_auth_string" {
  value     = google_redis_instance.rate_limits.auth_string
  sensitive = true
}

output "redis_secondary_host" {
  value = try(google_redis_instance.rate_limits_secondary[0].host, null)
}

output "redis_secondary_port" {
  value = try(google_redis_instance.rate_limits_secondary[0].port, null)
}

output "redis_secondary_ca_cert" {
  value     = try(google_redis_instance.rate_limits_secondary[0].server_ca_certs[0].cert, null)
  sensitive = true
}

output "redis_secondary_auth_string" {
  value     = try(google_redis_instance.rate_limits_secondary[0].auth_string, null)
  sensitive = true
}

output "get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.inference.name} --region ${var.region} --project ${var.project_id}"
}

output "public_api_url" {
  value = try(google_cloud_run_v2_service.public_api[0].uri, null)
}

output "secondary_api_url" {
  value = try(google_cloud_run_v2_service.public_api_secondary[0].uri, null)
}

output "canary_api_url" {
  value = try(google_cloud_run_v2_service.api_canary[0].uri, null)
}

output "production_edge_ip" {
  value = try(google_compute_global_address.public_api[0].address, null)
}

output "production_edge_dns_authorization" {
  value = try(google_certificate_manager_dns_authorization.public_api[0].dns_resource_record[0], null)
}

output "github_workload_identity_provider" {
  value = try(google_iam_workload_identity_pool_provider.github[0].name, null)
}

output "github_deployer_service_account" {
  value = try(google_service_account.github_deployer[0].email, null)
}

output "firestore_backup_bucket" {
  value = try(google_storage_bucket.firestore_backups[0].name, null)
}

output "firestore_backup_scheduler_job" {
  value = try(google_cloud_scheduler_job.firestore_export[0].id, null)
}

output "firestore_active_database" {
  value = local.active_firestore_database_id
}

output "firestore_active_backup_bucket" {
  value = try(local.active_firestore_backup_bucket, null)
}
