# Binary Authorization: only images from the project's Artifact Registry and
# the explicitly pinned public model repository may run. Enforcement is opted
# into per workload: the inference GKE cluster and the gateway Cloud Run
# services enforce the project policy; other clusters in the project (for
# example, the unrelated centaur cluster) are unaffected until they opt in.
#
# This is the pattern-allowlist tier of supply-chain control. Attestor-based
# signature enforcement is the follow-on hardening once the CI pipeline signs
# images.

resource "google_binary_authorization_policy" "policy" {
  count = var.enable_binary_authorization ? 1 : 0

  # Exempts Google-maintained GKE system images so cluster upgrades keep
  # working; it does not exempt anything user-deployed.
  global_policy_evaluation_mode = "ENABLE"

  admission_whitelist_patterns {
    name_pattern = "${var.region}-docker.pkg.dev/${var.project_id}/sovereign-inference/*"
  }

  admission_whitelist_patterns {
    name_pattern = "${var.region}-docker.pkg.dev/${var.project_id}/sovereign-inference/**"
  }

  # The pinned vLLM model server images; manifests reference them by digest.
  admission_whitelist_patterns {
    name_pattern = "docker.io/vllm/*"
  }

  admission_whitelist_patterns {
    name_pattern = "docker.io/vllm/**"
  }

  default_admission_rule {
    evaluation_mode  = "ALWAYS_DENY"
    enforcement_mode = "ENFORCED_BLOCK_AND_AUDIT_LOG"
  }

  depends_on = [google_project_service.required["binaryauthorization.googleapis.com"]]
}
