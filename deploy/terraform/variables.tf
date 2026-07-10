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

variable "gateway_image" {
  description = "Digest-pinned gateway container deployed to Cloud Run."
  type        = string
  default     = "us-central1-docker.pkg.dev/autonomous-lab-490610/sovereign-inference/gateway@sha256:86a57c217b396dcf0927291a2343dee0cda24db538bd3175b58b58f565aa034d"
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
