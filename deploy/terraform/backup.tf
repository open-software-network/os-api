data "google_project" "current" {
  project_id = var.project_id
}

resource "google_firestore_database" "primary" {
  count = var.enable_firestore_backups ? 1 : 0

  project                           = var.project_id
  name                              = "(default)"
  location_id                       = var.region
  type                              = "FIRESTORE_NATIVE"
  concurrency_mode                  = "PESSIMISTIC"
  app_engine_integration_mode       = "DISABLED"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"
  delete_protection_state           = "DELETE_PROTECTION_ENABLED"
  deletion_policy                   = "ABANDON"

  depends_on = [google_project_service.required["firestore.googleapis.com"]]
}

resource "google_storage_bucket" "firestore_backups" {
  count = var.enable_firestore_backups ? 1 : 0

  project                     = var.project_id
  name                        = "${var.project_id}-os-api-firestore-backups"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = var.labels

  versioning {
    enabled = true
  }

  retention_policy {
    retention_period = var.firestore_backup_retention_days * 86400
    is_locked        = false
  }

  lifecycle_rule {
    condition {
      age = var.firestore_backup_delete_after_days
    }

    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required["storage.googleapis.com"]]
}

resource "google_service_account" "firestore_backup" {
  count = var.enable_firestore_backups ? 1 : 0

  project      = var.project_id
  account_id   = "os-api-firestore-backup"
  display_name = "OpenSoftware API Firestore backup scheduler"
}

resource "google_project_iam_member" "firestore_backup_export" {
  count = var.enable_firestore_backups ? 1 : 0

  project = var.project_id
  role    = "roles/datastore.importExportAdmin"
  member  = "serviceAccount:${google_service_account.firestore_backup[0].email}"
}

resource "google_storage_bucket_iam_member" "firestore_service_agent_backup" {
  count = var.enable_firestore_backups ? 1 : 0

  bucket = google_storage_bucket.firestore_backups[0].name
  role   = "roles/storage.admin"
  member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-firestore.iam.gserviceaccount.com"
}

resource "google_cloud_scheduler_job" "firestore_export" {
  count = var.enable_firestore_backups ? 1 : 0

  project          = var.project_id
  region           = var.region
  name             = "os-api-firestore-export"
  description      = "Daily metadata and metering export for OpenSoftware API disaster recovery"
  schedule         = "0 4 * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "180s"

  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
    max_retry_duration   = "900s"
    max_doublings        = 3
  }

  http_target {
    http_method = "POST"
    uri         = "https://firestore.googleapis.com/v1/projects/${var.project_id}/databases/(default):exportDocuments"
    body = base64encode(jsonencode({
      outputUriPrefix = "gs://${google_storage_bucket.firestore_backups[0].name}"
      collectionIds = [
        "sovereign_api_keys",
        "sovereign_audit_events",
        "sovereign_usage",
      ]
    }))

    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.firestore_backup[0].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_iam_member.firestore_backup_export,
    google_storage_bucket_iam_member.firestore_service_agent_backup,
    google_project_service.required["cloudscheduler.googleapis.com"],
  ]
}
