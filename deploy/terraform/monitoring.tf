locals {
  alert_notification_channels = concat(
    var.alert_notification_channels,
    var.alert_email != "" ? [google_monitoring_notification_channel.email[0].name] : [],
  )
}

resource "google_monitoring_notification_channel" "email" {
  count = var.enable_public_api && var.enable_monitoring && var.alert_email != "" ? 1 : 0

  display_name = "OpenSoftware API operations"
  type         = "email"

  labels = {
    email_address = var.alert_email
  }

  force_delete = false

  depends_on = [google_project_service.required["monitoring.googleapis.com"]]
}

resource "google_monitoring_uptime_check_config" "public_api" {
  count = var.enable_public_api && var.enable_monitoring ? 1 : 0

  display_name = "OpenSoftware API readiness"
  timeout      = "10s"
  period       = "60s"

  http_check {
    request_method = "GET"
    path           = "/readyz"
    port           = 443
    use_ssl        = true
    validate_ssl   = true

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      host       = var.public_api_domain
      project_id = var.project_id
    }
  }

  depends_on = [google_project_service.required["monitoring.googleapis.com"]]
}

resource "google_monitoring_alert_policy" "public_api_uptime" {
  count = var.enable_public_api && var.enable_monitoring ? 1 : 0

  display_name          = "OpenSoftware API is unavailable"
  combiner              = "OR"
  notification_channels = local.alert_notification_channels

  conditions {
    display_name = "Readiness check fails"

    condition_threshold {
      filter          = "resource.type = \"uptime_url\" AND metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id = \"${google_monitoring_uptime_check_config.public_api[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "120s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }

      trigger {
        count = 1
      }

      evaluation_missing_data = "EVALUATION_MISSING_DATA_ACTIVE"
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "The public readiness endpoint has failed for at least two minutes. Follow docs/operations.md before shifting or restarting traffic."
    mime_type = "text/markdown"
  }

  timeouts {
    create = "5m"
  }
}

resource "google_monitoring_alert_policy" "public_api_5xx" {
  count = var.enable_public_api && var.enable_monitoring ? 1 : 0

  display_name          = "OpenSoftware API 5xx error ratio"
  combiner              = "OR"
  notification_channels = local.alert_notification_channels

  conditions {
    display_name = "5xx responses exceed 5 percent"

    condition_threshold {
      filter             = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${google_cloud_run_v2_service.public_api[0].name}\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.label.response_code_class = \"5xx\""
      denominator_filter = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${google_cloud_run_v2_service.public_api[0].name}\" AND metric.type = \"run.googleapis.com/request_count\""
      comparison         = "COMPARISON_GT"
      threshold_value    = 0.05
      duration           = "300s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }

      denominator_aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }

    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "The gateway has returned more than 5% server errors for five minutes. Check Cloud Run logs, Redis, Firestore, and model backend health."
    mime_type = "text/markdown"
  }

  timeouts {
    create = "5m"
  }
}

resource "google_monitoring_alert_policy" "public_api_secondary_5xx" {
  count = var.enable_public_api && var.enable_monitoring && var.enable_multi_region_control_plane ? 1 : 0

  display_name          = "OpenSoftware API secondary-region 5xx error ratio"
  combiner              = "OR"
  notification_channels = local.alert_notification_channels

  conditions {
    display_name = "Secondary-region 5xx responses exceed 5 percent"

    condition_threshold {
      filter             = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${google_cloud_run_v2_service.public_api_secondary[0].name}\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.label.response_code_class = \"5xx\""
      denominator_filter = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${google_cloud_run_v2_service.public_api_secondary[0].name}\" AND metric.type = \"run.googleapis.com/request_count\""
      comparison         = "COMPARISON_GT"
      threshold_value    = 0.05
      duration           = "300s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }

      denominator_aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "The us-east4 gateway has returned more than 5% server errors for five minutes. Inspect the regional Cloud Run revision and dependency connectivity; remove only the affected serverless NEG from the edge during mitigation."
    mime_type = "text/markdown"
  }

  timeouts {
    create = "5m"
  }
}

resource "google_logging_metric" "wallet_charge_failures" {
  count = var.enable_public_api && var.enable_monitoring ? 1 : 0

  name        = "os_api_wallet_charge_failures"
  description = "Inference usage that could not be settled against the OpenSoftware wallet"
  filter      = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"^sovereign-api(-secondary)?$\" AND jsonPayload.event=\"wallet_charge_failed\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "api_auth_failures" {
  count = var.enable_public_api && var.enable_monitoring ? 1 : 0

  name        = "os_api_auth_failures"
  description = "Rejected Bearer authentication attempts against OpenSoftware API endpoints"
  filter      = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"^sovereign-api(-secondary)?$\" AND jsonPayload.event=\"http_request\" AND jsonPayload.status=401 AND jsonPayload.path=~\"^/v1/\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "api_auth_failures" {
  count = var.enable_public_api && var.enable_monitoring ? 1 : 0

  display_name          = "OpenSoftware API authentication failures spiked"
  combiner              = "OR"
  notification_channels = local.alert_notification_channels

  conditions {
    display_name = "More than 60 rejected API credentials per minute"

    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.api_auth_failures[0].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 60
      duration        = "0s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "Bearer authentication failures exceeded 60 per minute. Inspect source distribution at the load balancer, confirm no key leak or credential-stuffing campaign, and preserve request IDs without copying credentials. Cloud Armor automatically bans sources above the extreme-rate threshold."
    mime_type = "text/markdown"
  }

  timeouts {
    create = "5m"
  }
}

resource "google_logging_metric" "firestore_backup_failures" {
  count = var.enable_firestore_backups && var.enable_monitoring ? 1 : 0

  name        = "os_api_firestore_backup_failures"
  description = "Failed executions of the scheduled OpenSoftware API Firestore export"
  filter      = "resource.type=\"cloud_scheduler_job\" AND resource.labels.job_id=\"${google_cloud_scheduler_job.firestore_export[0].name}\" AND severity>=ERROR"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "firestore_backup_failures" {
  count = var.enable_firestore_backups && var.enable_monitoring ? 1 : 0

  display_name          = "OpenSoftware API Firestore backup failed"
  combiner              = "OR"
  notification_channels = local.alert_notification_channels

  conditions {
    display_name = "One or more scheduled export attempts failed"

    condition_threshold {
      filter          = "resource.type = \"cloud_scheduler_job\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.firestore_backup_failures[0].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "A scheduled Firestore managed export failed. Inspect Cloud Scheduler execution logs, then run the backup manually and confirm the export operation completed before closing the incident. Follow the backup recovery procedure in docs/operations.md."
    mime_type = "text/markdown"
  }

  timeouts {
    create = "5m"
  }
}

resource "google_monitoring_alert_policy" "wallet_charge_failures" {
  count = var.enable_public_api && var.enable_monitoring ? 1 : 0

  display_name          = "OpenSoftware API wallet settlement failed"
  combiner              = "OR"
  notification_channels = local.alert_notification_channels

  conditions {
    display_name = "One or more wallet charges failed"

    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.wallet_charge_failures[0].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "A metered inference response was delivered but the idempotent OpenSoftware wallet charge failed after retries. Reconcile the billing ID from the structured log."
    mime_type = "text/markdown"
  }

  timeouts {
    create = "5m"
  }
}
