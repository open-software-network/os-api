terraform {
  required_version = ">= 1.9.0"

  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.42, < 8.4"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Some APIs (for example Binary Authorization attestor IAM) reject
  # user-credential calls without an explicit quota project.
  user_project_override = true
  billing_project       = var.project_id
}
