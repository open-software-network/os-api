terraform {
  required_version = ">= 1.8.0"

  backend "gcs" {
    bucket = "autonomous-lab-490610-sovereign-tfstate"
    prefix = "platform"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.42, < 8.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
