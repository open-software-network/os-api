variable "project_id" {
  description = "GCP project that will own the inference platform."
  type        = string
}

variable "region" {
  description = "GCP region for the cluster and regional services."
  type        = string
  default     = "us-central1"
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
  default     = "1296538310"
}

variable "github_repository_owner_id" {
  description = "Immutable numeric GitHub organization ID allowed to deploy."
  type        = string
  default     = "278133536"
}

variable "gateway_image" {
  description = "Digest-pinned gateway container deployed to Cloud Run."
  type        = string
  default     = "us-central1-docker.pkg.dev/autonomous-lab-490610/sovereign-inference/gateway@sha256:409e0860e8d21b604945ec81b307591d81f43d233c360f8812fbf1628ab1599d"
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
