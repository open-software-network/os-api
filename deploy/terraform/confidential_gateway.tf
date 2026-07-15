locals {
  confidential_gateway_digest = try(regex("sha256:[0-9a-f]{64}$", var.gateway_image), "")
  confidential_secret_environment = jsonencode({
    API_KEY_HASHES           = "gateway-api-key-hashes"
    REDIS_URL                = "gateway-redis-url"
    OS_ACCOUNTS_CLIENT_ID    = "os-accounts-client-id"
    OS_ACCOUNTS_APP_API_KEY  = "os-accounts-app-api-key"
    SITE_BASIC_AUTH_PASSWORD = "site-basic-auth-password"
    PHALA_API_KEY            = "phala-api-key"
    VENICE_API_KEY           = "venice-api-key"
  })
  confidential_secret_environment_secondary = jsonencode({
    API_KEY_HASHES           = "gateway-api-key-hashes"
    REDIS_URL                = "gateway-redis-url-secondary"
    OS_ACCOUNTS_CLIENT_ID    = "os-accounts-client-id"
    OS_ACCOUNTS_APP_API_KEY  = "os-accounts-app-api-key"
    SITE_BASIC_AUTH_PASSWORD = "site-basic-auth-password"
    PHALA_API_KEY            = "phala-api-key"
    VENICE_API_KEY           = "venice-api-key"
  })
  confidential_secret_files_primary = jsonencode({
    "/tmp/os-api-secrets/models.yaml"  = "cloudrun-model-config"
    "/tmp/os-api-secrets/redis-ca.pem" = "gateway-redis-ca"
  })
  confidential_secret_files_secondary = jsonencode({
    "/tmp/os-api-secrets/models.yaml"  = "cloudrun-model-config-secondary"
    "/tmp/os-api-secrets/redis-ca.pem" = "gateway-redis-ca-secondary"
  })
}

check "confidential_gateway_image_is_pinned" {
  assert {
    condition     = !var.enable_confidential_gateway || local.confidential_gateway_digest != ""
    error_message = "Confidential Space requires gateway_image to be pinned by a sha256 digest."
  }
}

resource "google_service_account" "confidential_gateway_bootstrap" {
  count        = var.enable_confidential_gateway ? 1 : 0
  account_id   = "os-api-confidential-bootstrap"
  display_name = "Confidential Space bootstrap without application data access"
}

resource "google_project_iam_member" "confidential_gateway_bootstrap" {
  for_each = var.enable_confidential_gateway ? toset([
    "roles/artifactregistry.reader",
    "roles/confidentialcomputing.workloadUser",
    "roles/logging.logWriter",
  ]) : toset([])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.confidential_gateway_bootstrap[0].email}"
}

resource "google_iam_workload_identity_pool" "confidential_gateway" {
  count                     = var.enable_confidential_gateway ? 1 : 0
  workload_identity_pool_id = "os-api-confidential"
  display_name              = "Measured os-api gateways"
}

resource "google_iam_workload_identity_pool_provider" "confidential_gateway" {
  count                              = var.enable_confidential_gateway ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.confidential_gateway[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "google-attestation"
  display_name                       = "Google Cloud Attestation"

  attribute_mapping = {
    "google.subject"         = "assertion.submods.container.image_digest"
    "attribute.image_digest" = "assertion.submods.container.image_digest"
  }
  attribute_condition = join(" && ", [
    "assertion.swname == 'CONFIDENTIAL_SPACE'",
    "assertion.dbgstat == 'disabled-since-boot'",
    "assertion.hwmodel == 'GCP_INTEL_TDX'",
    "'STABLE' in assertion.submods.confidential_space.support_attributes",
    "assertion.submods.container.image_digest == '${local.confidential_gateway_digest}'",
    "'${google_service_account.confidential_gateway_bootstrap[0].email}' in assertion.google_service_accounts",
  ])

  oidc {
    issuer_uri        = "https://confidentialcomputing.googleapis.com/"
    allowed_audiences = ["https://sts.googleapis.com"]
  }
}

resource "google_service_account_iam_member" "confidential_gateway_impersonates_runtime" {
  count              = var.enable_confidential_gateway ? 1 : 0
  service_account_id = google_service_account.api_runtime.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.confidential_gateway[0].name}/attribute.image_digest/${local.confidential_gateway_digest}"
}

locals {
  confidential_gateway_common_metadata = var.enable_confidential_gateway ? {
    "tee-image-reference"                                    = var.gateway_image
    "tee-restart-policy"                                     = "Always"
    "tee-container-log-redirect"                             = "true"
    "tee-env-REQUIRE_CONFIDENTIAL_SPACE"                     = "true"
    "tee-env-GOOGLE_CLOUD_PROJECT"                           = var.project_id
    "tee-env-CONFIDENTIAL_SPACE_WIP_PROVIDER"                = google_iam_workload_identity_pool_provider.confidential_gateway[0].name
    "tee-env-CONFIDENTIAL_SPACE_IMPERSONATE_SERVICE_ACCOUNT" = google_service_account.api_runtime.email
    "tee-env-CONFIDENTIAL_SPACE_SECRET_ENV"                  = local.confidential_secret_environment
    "tee-env-MODEL_CONFIG_PATH"                              = "/tmp/os-api-secrets/models.yaml"
    "tee-env-REDIS_CA_CERT_PATH"                             = "/tmp/os-api-secrets/redis-ca.pem"
    "tee-env-ENABLE_MANAGED_KEYS"                            = "true"
    "tee-env-FIRESTORE_COLLECTION_PREFIX"                    = "sovereign"
    "tee-env-FIRESTORE_DATABASE_ID"                          = local.active_firestore_database_id
    "tee-env-VENICE_COMPAT_ENABLED"                          = "true"
    "tee-env-APP_ORIGIN"                                     = "https://${var.public_api_domain}"
    "tee-env-OS_ACCOUNTS_URL"                                = "https://accounts.opensoftware.co"
    "tee-env-OS_ACCOUNTS_API_URL"                            = "https://accounts-api.opensoftware.co"
    "tee-env-SITE_BASIC_AUTH_USERNAME"                       = "alongside"
    "tee-env-GATEWAY_ATTESTATION_AUDIENCE"                   = "https://june-api.opensoftware.co/os-api-gateway"
  } : {}
}

resource "google_compute_instance_template" "confidential_gateway_primary" {
  count        = var.enable_confidential_gateway ? 1 : 0
  name_prefix  = "os-api-confidential-primary-"
  machine_type = var.confidential_gateway_machine_type
  tags         = ["os-api-confidential-gateway"]

  disk {
    source_image = "projects/confidential-space-images/global/images/family/confidential-space"
    boot         = true
    auto_delete  = true
  }
  network_interface {
    subnetwork = google_compute_subnetwork.gke.id
  }
  confidential_instance_config {
    enable_confidential_compute = true
    confidential_instance_type  = "TDX"
  }
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
  scheduling {
    on_host_maintenance = "TERMINATE"
    automatic_restart   = true
  }
  service_account {
    email  = google_service_account.confidential_gateway_bootstrap[0].email
    scopes = ["cloud-platform"]
  }
  metadata = merge(local.confidential_gateway_common_metadata, {
    "tee-env-GATEWAY_REGION"                  = var.region
    "tee-env-CONFIDENTIAL_SPACE_SECRET_FILES" = local.confidential_secret_files_primary
  })
  lifecycle { create_before_destroy = true }
}

resource "google_compute_instance_template" "confidential_gateway_secondary" {
  count        = var.enable_confidential_gateway && var.enable_multi_region_control_plane ? 1 : 0
  name_prefix  = "os-api-confidential-secondary-"
  machine_type = var.confidential_gateway_machine_type
  tags         = ["os-api-confidential-gateway"]

  disk {
    source_image = "projects/confidential-space-images/global/images/family/confidential-space"
    boot         = true
    auto_delete  = true
  }
  network_interface {
    subnetwork = google_compute_subnetwork.gateway_secondary[0].id
  }
  confidential_instance_config {
    enable_confidential_compute = true
    confidential_instance_type  = "TDX"
  }
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
  scheduling {
    on_host_maintenance = "TERMINATE"
    automatic_restart   = true
  }
  service_account {
    email  = google_service_account.confidential_gateway_bootstrap[0].email
    scopes = ["cloud-platform"]
  }
  metadata = merge(local.confidential_gateway_common_metadata, {
    "tee-env-GATEWAY_REGION"                  = var.secondary_region
    "tee-env-CONFIDENTIAL_SPACE_SECRET_ENV"   = local.confidential_secret_environment_secondary
    "tee-env-CONFIDENTIAL_SPACE_SECRET_FILES" = local.confidential_secret_files_secondary
  })
  lifecycle { create_before_destroy = true }
}

resource "google_compute_health_check" "confidential_gateway" {
  count               = var.enable_confidential_gateway ? 1 : 0
  name                = "os-api-confidential-gateway"
  timeout_sec         = 3
  check_interval_sec  = 10
  healthy_threshold   = 2
  unhealthy_threshold = 3
  http_health_check {
    port         = 8080
    request_path = "/readyz"
  }
}

resource "google_compute_region_instance_group_manager" "confidential_gateway_primary" {
  count                     = var.enable_confidential_gateway ? 1 : 0
  name                      = "os-api-confidential-primary"
  region                    = var.region
  base_instance_name        = "os-api-confidential-primary"
  target_size               = 1
  distribution_policy_zones = [var.confidential_gateway_primary_zone]
  version { instance_template = google_compute_instance_template.confidential_gateway_primary[0].id }
  named_port {
    name = "http"
    port = 8080
  }
  auto_healing_policies {
    health_check      = google_compute_health_check.confidential_gateway[0].id
    initial_delay_sec = 120
  }
}

resource "google_compute_region_instance_group_manager" "confidential_gateway_secondary" {
  count                     = var.enable_confidential_gateway && var.enable_multi_region_control_plane ? 1 : 0
  name                      = "os-api-confidential-secondary"
  region                    = var.secondary_region
  base_instance_name        = "os-api-confidential-secondary"
  target_size               = 1
  distribution_policy_zones = [var.confidential_gateway_secondary_zone]
  version { instance_template = google_compute_instance_template.confidential_gateway_secondary[0].id }
  named_port {
    name = "http"
    port = 8080
  }
  auto_healing_policies {
    health_check      = google_compute_health_check.confidential_gateway[0].id
    initial_delay_sec = 120
  }
}

resource "google_compute_firewall" "confidential_gateway_edge" {
  count         = var.enable_confidential_gateway ? 1 : 0
  name          = "os-api-confidential-gateway-edge"
  network       = google_compute_network.inference.name
  source_ranges = ["35.191.0.0/16", "130.211.0.0/22"]
  target_tags   = ["os-api-confidential-gateway"]
  allow {
    protocol = "tcp"
    ports    = ["8080"]
  }
}
