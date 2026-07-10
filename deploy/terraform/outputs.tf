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

output "get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.inference.name} --region ${var.region} --project ${var.project_id}"
}

output "public_api_url" {
  value = try(google_cloud_run_v2_service.public_api[0].uri, null)
}
