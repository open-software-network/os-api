# Second-region model plane: a zonal GKE cluster in us-east4-a running the
# same digest-pinned confidential CPU vLLM workload behind its own internal
# load balancer. Each region's gateway talks to its regional model backend
# (per-region cloudrun-model-config secrets), so a us-central1 loss no longer
# takes down inference everywhere. TDX C3 machines are available in
# us-east4-a and us-east4-c.
#
# Approximate steady cost while enabled: one c3-standard-4 node (~$140/mo)
# plus the GKE cluster fee (~$73/mo). Set enable_multi_region_model_plane to
# false and apply to tear it all down; Firestore, Redis, and the gateways are
# unaffected.

resource "google_compute_subnetwork" "gke_secondary" {
  count = var.enable_multi_region_model_plane ? 1 : 0

  name                     = "${var.cluster_name}-gke-${var.secondary_region}"
  region                   = var.secondary_region
  network                  = google_compute_network.inference.id
  ip_cidr_range            = "10.40.0.0/20"
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "gke-pods"
    ip_cidr_range = "10.44.0.0/14"
  }

  secondary_ip_range {
    range_name    = "gke-services"
    ip_cidr_range = "10.48.0.0/20"
  }
}

resource "google_compute_router" "nat_secondary" {
  count = var.enable_multi_region_model_plane ? 1 : 0

  name    = "${var.cluster_name}-router-${var.secondary_region}"
  region  = var.secondary_region
  network = google_compute_network.inference.id
}

resource "google_compute_router_nat" "private_nodes_secondary" {
  count = var.enable_multi_region_model_plane ? 1 : 0

  name                               = "${var.cluster_name}-nat-${var.secondary_region}"
  router                             = google_compute_router.nat_secondary[0].name
  region                             = var.secondary_region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.gke_secondary[0].id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
}

resource "google_container_cluster" "inference_secondary" {
  count = var.enable_multi_region_model_plane ? 1 : 0

  name     = "${var.cluster_name}-${var.secondary_region}"
  location = "${var.secondary_region}-a"

  network    = google_compute_network.inference.id
  subnetwork = google_compute_subnetwork.gke_secondary[0].id

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
    master_ipv4_cidr_block  = "172.16.0.16/28"
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

  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]

    managed_prometheus {
      enabled = true
    }
  }

  resource_labels = var.labels

  depends_on = [
    google_compute_router_nat.private_nodes_secondary,
    google_project_service.required,
    google_project_iam_member.node_roles,
  ]

  lifecycle {
    ignore_changes = [min_master_version]
  }
}

resource "google_container_node_pool" "confidential_cpu_secondary" {
  count = var.enable_multi_region_model_plane && var.enable_confidential_cpu_pool ? 1 : 0

  name           = "confidential-cpu-tdx"
  cluster        = google_container_cluster.inference_secondary[0].id
  node_locations = ["${var.secondary_region}-a"]

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

output "get_credentials_command_secondary" {
  value = (
    var.enable_multi_region_model_plane
    ? "gcloud container clusters get-credentials ${google_container_cluster.inference_secondary[0].name} --location ${google_container_cluster.inference_secondary[0].location} --project ${var.project_id}"
    : null
  )
}
