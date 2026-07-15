variable "project_id" {
  description = "GCP project that will own the inference platform."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid Google Cloud project ID."
  }
}

variable "region" {
  description = "GCP region for the cluster and regional services."
  type        = string
  default     = "us-central1"
}

variable "secondary_region" {
  description = "Second Cloud Run gateway region used by the global production edge."
  type        = string
  default     = "us-east4"
}

variable "system_node_zone" {
  description = "Zone for the system and ordinary GPU node pools."
  type        = string
  default     = "us-central1-a"
}

variable "standard_gpu_zones" {
  description = "Zones used by the ordinary GPU pool. Multiple zones reduce failures from temporary accelerator exhaustion."
  type        = list(string)
  default     = ["us-central1-a"]
}

variable "confidential_gpu_zone" {
  description = "A zone in the region that currently supports confidential H100 nodes."
  type        = string
  default     = "us-central1-a"
}

variable "master_authorized_cidr" {
  description = "CIDR allowed to reach the GKE control-plane public endpoint. Use a VPN/office egress range, never 0.0.0.0/0."
  type        = string

  validation {
    condition     = var.master_authorized_cidr != "0.0.0.0/0"
    error_message = "The GKE control plane must not be open to the entire internet."
  }
}

variable "cluster_name" {
  type    = string
  default = "sovereign-inference"
}

variable "enable_standard_gpu_pool" {
  type    = bool
  default = true
}

variable "enable_confidential_gpu_pool" {
  description = "Create the Spot H100 + TDX pool. Enable only after preemptible H100 quota is approved."
  type        = bool
  default     = false
}

variable "enable_confidential_cpu_pool" {
  description = "Create a scale-to-zero C3 + Intel TDX pool for low-cost confidential routing tests."
  type        = bool
  default     = false
}

variable "enable_public_api" {
  description = "Expose the API through Cloud Run's managed HTTPS endpoint. Runtime secrets must have active versions first."
  type        = bool
  default     = false
}

variable "enable_confidential_gateway" {
  description = "Run the public gateway as a measured Confidential Space workload instead of routing requests to Cloud Run. The image must be pinned by sha256 digest."
  type        = bool
  default     = false
}

variable "confidential_gateway_machine_type" {
  description = "Intel TDX machine type used by each Confidential Space gateway replica."
  type        = string
  default     = "c3-standard-4"
}

variable "confidential_gateway_primary_zone" {
  description = "Primary-region zone where the Confidential Space MIG may place its pinned TDX replica."
  type        = string
  default     = "us-central1-a"
}

variable "confidential_gateway_secondary_zone" {
  description = "Secondary-region zone where the Confidential Space MIG may place its pinned TDX replica."
  type        = string
  default     = "us-east4-a"
}

variable "public_api_domain" {
  description = "Public hostname for the API and developer portal."
  type        = string
  default     = "api.opensoftware.co"
}

variable "public_api_min_instances" {
  description = "Warm Cloud Run instances kept ready to avoid cold starts."
  type        = number
  default     = 1

  validation {
    condition     = var.public_api_min_instances >= 0
    error_message = "public_api_min_instances cannot be negative."
  }
}

variable "public_api_max_instances" {
  description = "Maximum Cloud Run gateway instances. Backend admission control remains independent."
  type        = number
  default     = 10

  validation {
    condition     = var.public_api_max_instances >= var.public_api_min_instances
    error_message = "public_api_max_instances must be at least public_api_min_instances."
  }
}

variable "enable_production_edge" {
  description = "Provision a global HTTPS load balancer, Certificate Manager certificate, and Cloud Armor policy."
  type        = bool
  default     = false
}

variable "restrict_cloud_run_ingress" {
  description = "Reject direct internet traffic that bypasses the production load balancer. Enable only after DNS cutover."
  type        = bool
  default     = false
}

variable "enable_legacy_domain_mapping" {
  description = "Keep the preview Cloud Run domain mapping during the load-balancer migration."
  type        = bool
  default     = true
}

variable "enable_monitoring" {
  description = "Create uptime checks and alert policies for the public API."
  type        = bool
  default     = true
}

variable "enable_firestore_backups" {
  description = "Schedule managed Firestore exports to a retention-protected bucket. The active public database always has PITR enabled and existing databases must be imported before apply."
  type        = bool
  default     = false
}

variable "firestore_collection_prefix" {
  description = "Prefix shared by every runtime Firestore collection, composite index, and scheduled export. Changing it points the gateway at a different logical dataset."
  type        = string
  default     = "sovereign"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{0,62}$", var.firestore_collection_prefix))
    error_message = "firestore_collection_prefix must start with a lowercase letter and contain at most 63 lowercase letters, digits, underscores, or hyphens."
  }
}

variable "enable_multi_region_control_plane" {
  description = "Create a secondary Cloud Run gateway and a multi-region Firestore system of record."
  type        = bool
  default     = false
}

variable "enable_multi_region_model_plane" {
  description = "Run a second confidential CPU model cluster in the secondary region so inference survives a primary-region loss. Adds roughly one c3-standard-4 node plus a GKE cluster fee while enabled."
  type        = bool
  default     = false
}

variable "operator_email" {
  description = "Human operator account granted release-signing rights for manual releases and drills."
  type        = string
  default     = ""
}

variable "site_basic_auth_username" {
  description = "Optional HTTP Basic username for the human-facing site. Leave empty to disable the site gate."
  type        = string
  default     = ""
}

variable "attestation_agent_url" {
  description = "Internal URL of the TDX quote agent (deploy/kubernetes/attestation). Empty disables /v1/attestation, which then fails closed. If the agent's internal LB IP changes, update this value."
  type        = string
  default     = ""
}

variable "attestation_agent_url_secondary" {
  description = "Internal URL of the TDX quote agent serving the secondary region."
  type        = string
  default     = ""
}

variable "enable_binary_authorization" {
  description = "Require release attestations for non-system images on the inference cluster and gateway Cloud Run services."
  type        = bool
  default     = false
}

variable "multi_region_firestore_database_id" {
  description = "Named Firestore database used by the multi-region control plane."
  type        = string
  default     = "os-api-global"
}

variable "multi_region_firestore_location" {
  description = "Firestore multi-region whose read-write regions align with the gateway regions."
  type        = string
  default     = "nam7"
}

variable "firestore_backup_retention_days" {
  description = "Minimum number of days that Firestore export objects cannot be deleted."
  type        = number
  default     = 30

  validation {
    condition     = var.firestore_backup_retention_days >= 7
    error_message = "Firestore backup retention must be at least seven days."
  }
}

variable "firestore_backup_delete_after_days" {
  description = "Age at which retained Firestore exports are removed automatically."
  type        = number
  default     = 90

  validation {
    condition     = var.firestore_backup_delete_after_days > var.firestore_backup_retention_days
    error_message = "Firestore backup deletion age must exceed its retention period."
  }
}

variable "alert_notification_channels" {
  description = "Cloud Monitoring notification channel resource names attached to API alerts."
  type        = list(string)
  default     = []
}

variable "alert_email" {
  description = "Optional email address that receives production alert notifications."
  type        = string
  default     = ""
}

variable "enable_github_deployments" {
  description = "Create keyless GitHub Actions deployment identity restricted to this repository and main branch."
  type        = bool
  default     = false
}

variable "github_repository_id" {
  description = "Immutable numeric GitHub repository ID allowed to deploy."
  type        = string
  default     = ""

  validation {
    condition     = var.github_repository_id == "" || can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id must be an immutable numeric GitHub repository ID."
  }
}

variable "github_repository_owner_id" {
  description = "Immutable numeric GitHub organization ID allowed to deploy."
  type        = string
  default     = ""

  validation {
    condition     = var.github_repository_owner_id == "" || can(regex("^[0-9]+$", var.github_repository_owner_id))
    error_message = "github_repository_owner_id must be an immutable numeric GitHub organization ID."
  }
}

variable "gateway_image" {
  description = "Digest-pinned gateway container deployed to Cloud Run."
  type        = string
  default     = ""
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "labels" {
  type = map(string)
  default = {
    application = "sovereign-inference"
    managed-by  = "terraform"
  }
}
