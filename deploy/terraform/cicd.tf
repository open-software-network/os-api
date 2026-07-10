resource "google_service_account" "github_deployer" {
  count = var.enable_github_deployments ? 1 : 0

  account_id   = "os-api-github-deployer"
  display_name = "OpenSoftware API GitHub deployer"
}

resource "google_iam_workload_identity_pool" "github" {
  count = var.enable_github_deployments ? 1 : 0

  workload_identity_pool_id = "os-api-github"
  display_name              = "OpenSoftware API GitHub"

  depends_on = [google_project_service.required["iam.googleapis.com"]]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = var.enable_github_deployments ? 1 : 0

  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "os-api main branch"

  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
  }

  attribute_condition = "assertion.repository_owner_id == '${var.github_repository_owner_id}' && assertion.repository_id == '${var.github_repository_id}' && assertion.ref == 'refs/heads/main'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_identity" {
  count = var.enable_github_deployments ? 1 : 0

  service_account_id = google_service_account.github_deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository_id/${var.github_repository_id}"
}

resource "google_artifact_registry_repository_iam_member" "github_writer" {
  count = var.enable_github_deployments ? 1 : 0

  location   = google_artifact_registry_repository.containers.location
  repository = google_artifact_registry_repository.containers.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_deployer[0].email}"
}

resource "google_project_iam_member" "github_run_developer" {
  count = var.enable_github_deployments ? 1 : 0

  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.github_deployer[0].email}"
}

resource "google_service_account_iam_member" "github_runtime_user" {
  count = var.enable_github_deployments ? 1 : 0

  service_account_id = google_service_account.api_runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_deployer[0].email}"
}

resource "google_secret_manager_secret_iam_member" "github_smoke_key" {
  count = var.enable_github_deployments ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.runtime["smoke-client-api-key"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.github_deployer[0].email}"
}
