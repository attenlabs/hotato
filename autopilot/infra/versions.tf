terraform {
  required_version = ">= 1.6"

  required_providers {
    google      = { source = "hashicorp/google", version = "~> 6.0" }
    google-beta = { source = "hashicorp/google-beta", version = "~> 6.0" }
    archive     = { source = "hashicorp/archive", version = "~> 2.4" }
  }

  # Backend is configured at init time by the owner (bucket created by bootstrap in RUNBOOK).
  # `terraform init -backend=false` validates without it.
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
