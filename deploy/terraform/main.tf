locals {
  services = toset([
    "artifactregistry.googleapis.com",
    "binaryauthorization.googleapis.com",
    "compute.googleapis.com",
    "confidentialcomputing.googleapis.com",
    "container.googleapis.com",
    "certificatemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "containeranalysis.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "redis.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "siteverification.googleapis.com",
    "storage.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each = local.services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "inference" {
  name                    = "${var.cluster_name}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.required]
}

resource "google_compute_subnetwork" "gke" {
  name                     = "${var.cluster_name}-gke"
  region                   = var.region
  network                  = google_compute_network.inference.id
  ip_cidr_range            = "10.20.0.0/20"
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "gke-pods"
    ip_cidr_range = "10.24.0.0/14"
  }

  secondary_ip_range {
    range_name    = "gke-services"
    ip_cidr_range = "10.28.0.0/20"
  }
}

resource "google_compute_router" "nat" {
  name    = "${var.cluster_name}-router"
  region  = var.region
  network = google_compute_network.inference.id
}

resource "google_compute_router_nat" "private_nodes" {
  name                               = "${var.cluster_name}-nat"
  router                             = google_compute_router.nat.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.gke.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

resource "google_service_account" "gke_nodes" {
  account_id   = "sovereign-gke-nodes"
  display_name = "Sovereign Inference GKE nodes"
}

resource "google_project_iam_member" "node_roles" {
  for_each = toset([
    "roles/artifactregistry.reader",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_artifact_registry_repository" "containers" {
  location      = var.region
  repository_id = "sovereign-inference"
  description   = "Signed inference platform images"
  format        = "DOCKER"
  labels        = var.labels

  depends_on = [google_project_service.required]
}

resource "google_redis_instance" "rate_limits" {
  name                    = "${var.cluster_name}-limits"
  region                  = var.region
  tier                    = "STANDARD_HA"
  memory_size_gb          = 1
  redis_version           = "REDIS_7_2"
  authorized_network      = google_compute_network.inference.id
  connect_mode            = "DIRECT_PEERING"
  auth_enabled            = true
  transit_encryption_mode = "SERVER_AUTHENTICATION"
  labels                  = var.labels

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "runtime" {
  for_each = toset([
    "app-client-api-key",
    "cloudrun-model-config",
    "cloudrun-model-config-secondary",
    "gateway-api-key-hashes",
    "gateway-redis-ca",
    "gateway-redis-ca-secondary",
    "gateway-redis-url",
    "gateway-redis-url-secondary",
    "os-accounts-app-api-key",
    "os-accounts-client-id",
    "site-basic-auth-password",
    "standard-vllm-api-key",
    "confidential-vllm-api-key",
    "huggingface-token",
    "smoke-client-api-key",
    "phala-api-key",
    "venice-api-key",
  ])

  secret_id = each.value

  replication {
    auto {}
  }

  labels = var.labels

  depends_on = [google_project_service.required]
}

resource "google_service_account" "api_runtime" {
  account_id   = "sovereign-api-runtime"
  display_name = "Sovereign Inference public API runtime"
}

resource "google_project_iam_member" "api_runtime_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.api_runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "api_runtime" {
  for_each = toset([
    "cloudrun-model-config",
    "cloudrun-model-config-secondary",
    "gateway-api-key-hashes",
    "gateway-redis-ca",
    "gateway-redis-ca-secondary",
    "gateway-redis-url",
    "gateway-redis-url-secondary",
    "os-accounts-app-api-key",
    "os-accounts-client-id",
    "site-basic-auth-password",
    "phala-api-key",
    "venice-api-key",
  ])

  project   = var.project_id
  secret_id = google_secret_manager_secret.runtime[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api_runtime.email}"
}

resource "google_cloud_run_v2_service" "public_api" {
  count = var.enable_public_api ? 1 : 0

  name     = "sovereign-api"
  location = var.region
  ingress = (
    var.enable_production_edge && var.restrict_cloud_run_ingress
    ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    : "INGRESS_TRAFFIC_ALL"
  )
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
      min_instance_count = var.public_api_min_instances
      max_instance_count = var.public_api_max_instances
    }

    containers {
      image = var.gateway_image

      ports {
        container_port = 8080
      }

      env {
        name  = "MODEL_CONFIG_PATH"
        value = "/app/config/models.yaml"
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
        name  = "ATTESTATION_AGENT_URL"
        value = var.attestation_agent_url
      }

      env {
        name  = "APP_ORIGIN"
        value = "https://api.opensoftware.co"
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

      # Bump when a `latest` runtime secret changes to force a fresh revision.
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

      env {
        name = "PHALA_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.runtime["phala-api-key"].secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "VENICE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.runtime["venice-api-key"].secret_id
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

  # CI owns digest-pinned application rollouts after the service is bootstrapped.
  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image,
      template[0].revision,
    ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "public_api" {
  count = var.enable_public_api ? 1 : 0

  project  = var.project_id
  location = google_cloud_run_v2_service.public_api[0].location
  name     = google_cloud_run_v2_service.public_api[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_domain_mapping" "public_api" {
  count = var.enable_public_api && var.enable_legacy_domain_mapping ? 1 : 0

  location = var.region
  name     = var.public_api_domain

  metadata {
    namespace = var.project_id
  }

  spec {
    route_name = google_cloud_run_v2_service.public_api[0].name
  }

  lifecycle {
    ignore_changes = [
      metadata[0].annotations,
      metadata[0].labels,
      spec[0].certificate_mode,
      spec[0].force_override,
    ]
  }
}

resource "google_container_cluster" "inference" {
  name     = var.cluster_name
  location = var.region

  network    = google_compute_network.inference.id
  subnetwork = google_compute_subnetwork.gke.id

  remove_default_node_pool   = true
  initial_node_count         = 1
  deletion_protection        = var.deletion_protection
  networking_mode            = "VPC_NATIVE"
  datapath_provider          = "ADVANCED_DATAPATH"
  enable_fqdn_network_policy = true
  enable_shielded_nodes      = true

  release_channel {
    channel = "RAPID"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "gke-pods"
    services_secondary_range_name = "gke-services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  master_authorized_networks_config {
    cidr_blocks {
      cidr_block   = var.master_authorized_cidr
      display_name = "operator-network"
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  dynamic "binary_authorization" {
    for_each = var.enable_binary_authorization ? [1] : []

    content {
      evaluation_mode = "PROJECT_SINGLETON_POLICY_ENFORCE"
    }
  }

  secret_manager_config {
    enabled = true
  }

  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "APISERVER", "SCHEDULER", "CONTROLLER_MANAGER"]

    managed_prometheus {
      enabled = true
    }
  }

  resource_labels = var.labels

  depends_on = [
    google_compute_router_nat.private_nodes,
    google_project_service.required,
    google_project_iam_member.node_roles,
  ]

  # The release channel owns patch upgrades; avoid coupling unrelated applies
  # to the control plane's server-selected current version.
  lifecycle {
    ignore_changes = [min_master_version]
  }
}

resource "google_container_node_pool" "system" {
  name           = "system"
  cluster        = google_container_cluster.inference.id
  node_locations = [var.system_node_zone]

  autoscaling {
    min_node_count = 1
    max_node_count = 3
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = "e2-standard-4"
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = merge(var.labels, { workload = "system" })

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

resource "google_container_node_pool" "standard_gpu" {
  count = var.enable_standard_gpu_pool ? 1 : 0

  name           = "standard-l4"
  cluster        = google_container_cluster.inference.id
  node_locations = var.standard_gpu_zones

  autoscaling {
    total_min_node_count = 0
    total_max_node_count = 4
    location_policy      = "ANY"
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = "g2-standard-8"
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = merge(var.labels, { workload = "standard-gpu" })

    guest_accelerator {
      type  = "nvidia-l4"
      count = 1

      gpu_driver_installation_config {
        gpu_driver_version = "LATEST"
      }
    }

    taint {
      key    = "dedicated"
      value  = "standard-gpu"
      effect = "NO_SCHEDULE"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

resource "google_container_node_pool" "confidential_cpu" {
  count = var.enable_confidential_cpu_pool ? 1 : 0

  name           = "confidential-cpu-tdx"
  cluster        = google_container_cluster.inference.id
  node_locations = [var.system_node_zone]

  autoscaling {
    min_node_count = 0
    max_node_count = 2
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = "c3-standard-4"
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = merge(var.labels, { workload = "confidential-cpu" })

    confidential_nodes {
      enabled                    = true
      confidential_instance_type = "TDX"
    }

    taint {
      key    = "dedicated"
      value  = "confidential-cpu"
      effect = "NO_SCHEDULE"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

resource "google_container_node_pool" "confidential_gpu" {
  count = var.enable_confidential_gpu_pool ? 1 : 0

  name           = "confidential-h100-tdx"
  cluster        = google_container_cluster.inference.id
  node_locations = [var.confidential_gpu_zone]

  autoscaling {
    min_node_count = 0
    max_node_count = 2
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = "a3-highgpu-1g"
    image_type      = "COS_CONTAINERD"
    spot            = true
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = merge(var.labels, { workload = "confidential-gpu" })

    confidential_nodes {
      enabled                    = true
      confidential_instance_type = "TDX"
    }

    guest_accelerator {
      type  = "nvidia-h100-80gb"
      count = 1

      gpu_driver_installation_config {
        gpu_driver_version = "LATEST"
      }
    }

    taint {
      key    = "dedicated"
      value  = "confidential-gpu"
      effect = "NO_SCHEDULE"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  timeouts {
    create = "60m"
    update = "60m"
  }
}
