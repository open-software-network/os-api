locals {
  provision_confidential_public_api = var.enable_public_api && var.enable_production_edge && var.enable_confidential_gateway
  use_confidential_public_api       = local.provision_confidential_public_api && var.route_public_api_to_confidential_gateway
}

resource "google_compute_global_address" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name       = "os-api-edge"
  ip_version = "IPV4"
}

resource "google_certificate_manager_dns_authorization" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name        = "os-api-domain-authorization"
  description = "DNS authorization for the OpenSoftware API edge certificate"
  domain      = var.public_api_domain

  depends_on = [google_project_service.required["certificatemanager.googleapis.com"]]
}

resource "google_certificate_manager_certificate" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name        = "os-api-edge"
  description = "Google-managed certificate for ${var.public_api_domain}"

  managed {
    domains            = [var.public_api_domain]
    dns_authorizations = [google_certificate_manager_dns_authorization.public_api[0].id]
  }
}

resource "google_certificate_manager_certificate_map" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name        = "os-api-edge"
  description = "Certificate map for the OpenSoftware API edge"

  depends_on = [google_project_service.required["certificatemanager.googleapis.com"]]
}

resource "google_certificate_manager_certificate_map_entry" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name         = "os-api-edge"
  map          = google_certificate_manager_certificate_map.public_api[0].name
  certificates = [google_certificate_manager_certificate.public_api[0].id]
  hostname     = var.public_api_domain
}

resource "google_compute_region_network_endpoint_group" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name                  = "os-api-cloud-run"
  network_endpoint_type = "SERVERLESS"
  region                = var.region

  cloud_run {
    service = google_cloud_run_v2_service.public_api[0].name
  }
}

resource "google_compute_security_policy" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name        = "os-api-edge"
  description = "Availability protection for the OpenSoftware API"
  type        = "CLOUD_ARMOR"

  rule {
    action      = "rate_based_ban"
    priority    = 900
    description = "Protect the public nonce-bound gateway attestation signer from request amplification"

    match {
      expr {
        expression = "request.path == '/v1/gateway/attestation'"
      }
    }

    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"

      rate_limit_threshold {
        count        = 30
        interval_sec = 60
      }

      ban_threshold {
        count        = 90
        interval_sec = 60
      }

      ban_duration_sec = 600
    }
  }

  rule {
    action      = "rate_based_ban"
    priority    = 1000
    description = "Throttle abusive inference traffic and temporarily ban extreme sources; API-key quotas remain authoritative"

    match {
      expr {
        expression = "request.path.startsWith('/v1/')"
      }
    }

    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"

      rate_limit_threshold {
        count        = 600
        interval_sec = 60
      }

      ban_threshold {
        count        = 1800
        interval_sec = 60
      }

      ban_duration_sec = 600
    }
  }

  rule {
    action      = "allow"
    priority    = 2147483647
    description = "Default allow"

    match {
      versioned_expr = "SRC_IPS_V1"

      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

resource "google_compute_backend_service" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name                  = "os-api-edge"
  protocol              = "HTTP"
  port_name             = "http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.public_api[0].id

  backend {
    group = google_compute_region_network_endpoint_group.public_api[0].id
  }

  dynamic "backend" {
    for_each = var.enable_multi_region_control_plane ? [1] : []

    content {
      group = google_compute_region_network_endpoint_group.public_api_secondary[0].id
    }
  }

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_backend_service" "confidential_public_api" {
  count = local.provision_confidential_public_api ? 1 : 0

  name                  = "os-api-edge-confidential"
  protocol              = "HTTP"
  port_name             = "http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.public_api[0].id
  timeout_sec           = 620

  backend {
    group           = google_compute_region_instance_group_manager.confidential_gateway_primary[0].instance_group
    balancing_mode  = "UTILIZATION"
    max_utilization = 0.8
  }

  dynamic "backend" {
    for_each = var.enable_multi_region_control_plane ? [1] : []

    content {
      group           = google_compute_region_instance_group_manager.confidential_gateway_secondary[0].instance_group
      balancing_mode  = "UTILIZATION"
      max_utilization = 0.8
    }
  }

  health_checks = [google_compute_health_check.confidential_gateway[0].id]

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_url_map" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name            = "os-api-edge"
  default_service = local.use_confidential_public_api ? google_compute_backend_service.confidential_public_api[0].id : google_compute_backend_service.public_api[0].id
}

resource "google_compute_target_https_proxy" "public_api" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name            = "os-api-edge"
  url_map         = google_compute_url_map.public_api[0].id
  certificate_map = "//certificatemanager.googleapis.com/${google_certificate_manager_certificate_map.public_api[0].id}"
  quic_override   = "ENABLE"
}

resource "google_compute_global_forwarding_rule" "public_api_https" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name                  = "os-api-edge-https"
  ip_address            = google_compute_global_address.public_api[0].id
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_https_proxy.public_api[0].id
}

resource "google_compute_url_map" "public_api_redirect" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name = "os-api-edge-http-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "public_api_redirect" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name    = "os-api-edge-http-redirect"
  url_map = google_compute_url_map.public_api_redirect[0].id
}

resource "google_compute_global_forwarding_rule" "public_api_http" {
  count = var.enable_public_api && var.enable_production_edge ? 1 : 0

  name                  = "os-api-edge-http"
  ip_address            = google_compute_global_address.public_api[0].id
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_http_proxy.public_api_redirect[0].id
}
