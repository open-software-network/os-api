resource "google_cloud_run_v2_service" "api_canary" {
  count = var.enable_public_api && var.enable_github_deployments ? 1 : 0

  name                = "sovereign-api-canary"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true
  labels              = var.labels

  dynamic "binary_authorization" {
    for_each = var.enable_binary_authorization ? [1] : []

    content {
      use_default = true
    }
  }

  template {
    service_account                  = google_service_account.api_runtime.email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    max_instance_request_concurrency = 20
    timeout                          = "600s"

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.gateway_image

      ports {
        container_port = 8080
      }

      env {
        name  = "MODEL_CONFIG_PATH"
        value = "/etc/sovereign/models.yaml"
      }

      env {
        name  = "REDIS_CA_CERT_PATH"
        value = "/etc/redis/ca.pem"
      }

      env {
        name  = "REQUESTS_PER_MINUTE"
        value = "60"
      }

      env {
        name  = "MAX_REQUEST_BYTES"
        value = "10485760"
      }

      env {
        name  = "ENABLE_MANAGED_KEYS"
        value = "true"
      }

      env {
        name  = "FIRESTORE_COLLECTION_PREFIX"
        value = "sovereign"
      }

      env {
        name  = "FIRESTORE_DATABASE_ID"
        value = local.active_firestore_database_id
      }

      env {
        name  = "GATEWAY_REGION"
        value = var.region
      }

      env {
        name  = "APP_ORIGIN"
        value = "https://${var.public_api_domain}"
      }

      env {
        name  = "OS_ACCOUNTS_URL"
        value = "https://accounts.opensoftware.co"
      }

      env {
        name  = "OS_ACCOUNTS_API_URL"
        value = "https://accounts-api.opensoftware.co"
      }

      env {
        name  = "SITE_BASIC_AUTH_USERNAME"
        value = "alongside"
      }

      env {
        name  = "RUNTIME_CONFIG_VERSION"
        value = "9"
      }

      env {
        name = "API_KEY_HASHES"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.runtime["gateway-api-key-hashes"].secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "REDIS_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.runtime["gateway-redis-url"].secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "OS_ACCOUNTS_CLIENT_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.runtime["os-accounts-client-id"].secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "OS_ACCOUNTS_APP_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.runtime["os-accounts-app-api-key"].secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "SITE_BASIC_AUTH_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.runtime["site-basic-auth-password"].secret_id
            version = "latest"
          }
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      startup_probe {
        initial_delay_seconds = 0
        timeout_seconds       = 3
        period_seconds        = 3
        failure_threshold     = 20

        http_get {
          path = "/readyz"
          port = 8080
        }
      }

      liveness_probe {
        initial_delay_seconds = 10
        timeout_seconds       = 3
        period_seconds        = 30
        failure_threshold     = 3

        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      volume_mounts {
        name       = "models"
        mount_path = "/etc/sovereign"
      }

      volume_mounts {
        name       = "redis-ca"
        mount_path = "/etc/redis"
      }
    }

    volumes {
      name = "models"
      secret {
        secret       = google_secret_manager_secret.runtime["cloudrun-model-config"].secret_id
        default_mode = 292
        items {
          version = "latest"
          path    = "models.yaml"
        }
      }
    }

    volumes {
      name = "redis-ca"
      secret {
        secret       = google_secret_manager_secret.runtime["gateway-redis-ca"].secret_id
        default_mode = 292
        items {
          version = "latest"
          path    = "ca.pem"
        }
      }
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.inference.id
        subnetwork = google_compute_subnetwork.gke.id
        tags       = ["sovereign-api"]
      }
    }
  }

  depends_on = [
    google_project_service.required["run.googleapis.com"],
    google_project_iam_member.api_runtime_firestore,
    google_secret_manager_secret_iam_member.api_runtime,
  ]

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image,
      template[0].revision,
    ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "github_canary_invoker" {
  count = var.enable_public_api && var.enable_github_deployments ? 1 : 0

  project  = var.project_id
  location = google_cloud_run_v2_service.api_canary[0].location
  name     = google_cloud_run_v2_service.api_canary[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.github_deployer[0].email}"
}
