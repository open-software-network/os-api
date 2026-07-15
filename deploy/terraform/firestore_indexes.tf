locals {
  firestore_user_list_indexes = {
    api_keys = {
      collection      = "${var.firestore_collection_prefix}_api_keys"
      timestamp_field = "created_at"
    }
    usage = {
      collection      = "${var.firestore_collection_prefix}_usage"
      timestamp_field = "created_at"
    }
    audit_events = {
      collection      = "${var.firestore_collection_prefix}_audit_events"
      timestamp_field = "occurred_at"
    }
  }
}

resource "google_firestore_index" "user_lists" {
  for_each = var.enable_public_api ? local.firestore_user_list_indexes : {}

  project     = var.project_id
  database    = local.active_firestore_database_id
  collection  = each.value.collection
  query_scope = "COLLECTION"

  fields {
    field_path = "user_id"
    order      = "ASCENDING"
  }

  fields {
    field_path = each.value.timestamp_field
    order      = "DESCENDING"
  }

  depends_on = [
    google_firestore_database.primary,
    google_firestore_database.global,
  ]
}
