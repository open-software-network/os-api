# Binary Authorization requires every non-system workload image to carry a
# release attestation. Enforcement is opted into per workload: the inference
# GKE cluster and the gateway Cloud Run services enforce the project policy;
# other clusters in the project are unaffected until they opt in.

resource "google_binary_authorization_policy" "policy" {
  count = var.enable_binary_authorization ? 1 : 0

  # Exempts Google-maintained GKE system images so cluster upgrades keep
  # working; it does not exempt anything user-deployed.
  global_policy_evaluation_mode = "ENABLE"

  default_admission_rule {
    evaluation_mode  = "REQUIRE_ATTESTATION"
    enforcement_mode = "ENFORCED_BLOCK_AND_AUDIT_LOG"

    require_attestations_by = [
      google_binary_authorization_attestor.release[0].name,
    ]
  }

  depends_on = [google_project_service.required["binaryauthorization.googleapis.com"]]
}

resource "google_kms_key_ring" "binauthz" {
  count = var.enable_binary_authorization ? 1 : 0

  name     = "os-api-binauthz"
  location = var.region

  depends_on = [google_project_service.required["cloudkms.googleapis.com"]]
}

resource "google_kms_crypto_key" "attestor" {
  count = var.enable_binary_authorization ? 1 : 0

  name     = "release-signing"
  key_ring = google_kms_key_ring.binauthz[0].id
  purpose  = "ASYMMETRIC_SIGN"

  version_template {
    algorithm = "EC_SIGN_P256_SHA256"
  }
}

data "google_kms_crypto_key_version" "attestor" {
  count = var.enable_binary_authorization ? 1 : 0

  crypto_key = google_kms_crypto_key.attestor[0].id
}

resource "google_container_analysis_note" "release" {
  count = var.enable_binary_authorization ? 1 : 0

  name = "os-api-release-attestor"

  attestation_authority {
    hint {
      human_readable_name = "OpenSoftware API release attestor"
    }
  }

  depends_on = [google_project_service.required["containeranalysis.googleapis.com"]]
}

resource "google_binary_authorization_attestor" "release" {
  count = var.enable_binary_authorization ? 1 : 0

  name = "os-api-release"

  attestation_authority_note {
    note_reference = google_container_analysis_note.release[0].name

    public_keys {
      id = data.google_kms_crypto_key_version.attestor[0].id

      pkix_public_key {
        public_key_pem      = data.google_kms_crypto_key_version.attestor[0].public_key[0].pem
        signature_algorithm = data.google_kms_crypto_key_version.attestor[0].public_key[0].algorithm
      }
    }
  }
}

# The GitHub deploy workflow signs every image it builds.
resource "google_kms_crypto_key_iam_member" "deployer_signs" {
  count = var.enable_binary_authorization && var.enable_github_deployments ? 1 : 0

  crypto_key_id = google_kms_crypto_key.attestor[0].id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.github_deployer[0].email}"
}

resource "google_container_analysis_note_iam_member" "deployer_attaches" {
  count = var.enable_binary_authorization && var.enable_github_deployments ? 1 : 0

  note = google_container_analysis_note.release[0].name
  role = "roles/containeranalysis.notes.attacher"

  member = "serviceAccount:${google_service_account.github_deployer[0].email}"
}

# Attestations are stored as Container Analysis occurrences in the project.
# Note attachment permission above does not grant occurrence creation.
resource "google_project_iam_member" "deployer_writes_attestation_occurrences" {
  count = var.enable_binary_authorization && var.enable_github_deployments ? 1 : 0

  project = var.project_id
  role    = "roles/containeranalysis.occurrences.editor"
  member  = "serviceAccount:${google_service_account.github_deployer[0].email}"
}

resource "google_binary_authorization_attestor_iam_member" "deployer_views" {
  count = var.enable_binary_authorization && var.enable_github_deployments ? 1 : 0

  attestor = google_binary_authorization_attestor.release[0].name
  role     = "roles/binaryauthorization.attestorsViewer"
  member   = "serviceAccount:${google_service_account.github_deployer[0].email}"
}

# KMS data-plane operations are not part of roles/owner; the operator signs
# release digests during manual releases and drills.
resource "google_kms_crypto_key_iam_member" "operator_signs" {
  count = var.enable_binary_authorization && var.operator_email != "" ? 1 : 0

  crypto_key_id = google_kms_crypto_key.attestor[0].id
  role          = "roles/cloudkms.signerVerifier"
  member        = "user:${var.operator_email}"
}

resource "google_container_analysis_note_iam_member" "operator_attaches" {
  count = var.enable_binary_authorization && var.operator_email != "" ? 1 : 0

  note = google_container_analysis_note.release[0].name
  role = "roles/containeranalysis.notes.attacher"

  member = "user:${var.operator_email}"
}
