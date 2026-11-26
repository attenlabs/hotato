# Self-maintenance: the Terraform state bucket, infra changes that apply themselves from main,
# and the weekly image rebuild (its schedule is in schedule.tf).
#
# The Cloud Build triggers need the hotato GitHub repo connected to this project's Cloud Build
# (an owner step in RUNBOOK.md), so everything here except the state bucket exists only when
# self_maintain = true.

variable "self_maintain" {
  type        = bool
  default     = false
  description = "Create the infra apply/plan triggers and the weekly image rebuild. Needs the GitHub repo connected to Cloud Build first (RUNBOOK)."
}

variable "cloudflare_account_id" {
  type        = string
  default     = ""
  description = "Cloudflare account id for the site deploy step of the weekly rebuild. Required when self_maintain = true."
}

locals {
  tfstate_bucket = "${var.project_id}-tfstate"
  gh_owner       = split("/", var.github_repo)[0]
  gh_name        = split("/", var.github_repo)[1]
  gh_url         = "https://github.com/${var.github_repo}"
  infra_files    = ["autopilot/infra/**", "autopilot/build/cloudbuild-infra.yaml"]
  maintain       = var.self_maintain ? 1 : 0
}

# ---- Terraform state bucket ----
# the bootstrap script creates it (Terraform cannot create the bucket that holds its own state);
# this adopts it so its rules are kept as code. Only old, replaced versions are ever deleted:
# a version goes once it is both 30 days replaced AND has 10 newer versions, so the live state
# and the last 10 states are always kept.
import {
  to = google_storage_bucket.tfstate
  id = "${var.project_id}/${local.tfstate_bucket}"
}

resource "google_storage_bucket" "tfstate" {
  name                        = local.tfstate_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      with_state                 = "ARCHIVED"
      days_since_noncurrent_time = 30
      num_newer_versions         = 10
    }
    action {
      type = "Delete"
    }
  }
  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# ---- Identities ----
# Applies infra from main. It must manage IAM, so it holds owner on this project only. Only the
# main-branch trigger below runs as it, and autopilot/infra/** on main needs the owner's review (gates).
# The GitHub deployer identity is deliberately NOT allowed to act as it.
resource "google_service_account" "infra" {
  count        = local.maintain
  account_id   = "hotato-infra"
  display_name = "applies infra from main (Cloud Build trigger only)"
  depends_on   = [google_project_service.api]
}

resource "google_project_iam_member" "infra_owner" {
  count   = local.maintain
  project = var.project_id
  role    = "roles/owner"
  member  = "serviceAccount:${google_service_account.infra[0].email}"
}

# Plans pull requests: read-only, and it cannot take the state lock (plans run with -lock=false).
resource "google_service_account" "infra_plan" {
  count        = local.maintain
  account_id   = "hotato-infra-plan"
  display_name = "plans infra pull requests (read-only)"
  depends_on   = [google_project_service.api]
}

resource "google_project_iam_member" "infra_plan" {
  for_each = var.self_maintain ? toset([
    "roles/viewer", "roles/iam.securityReviewer", "roles/secretmanager.viewer",
    "roles/cloudbuild.builds.viewer", "roles/logging.logWriter",
  ]) : toset([])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.infra_plan[0].email}"
}

resource "google_storage_bucket_iam_member" "infra_plan_read" {
  for_each = var.self_maintain ? {
    tfstate = google_storage_bucket.tfstate.name
    state   = google_storage_bucket.state.name
  } : {}
  bucket = each.value
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.infra_plan[0].email}"
}

# Cloud Scheduler identity for the weekly rebuild: may start builds and run them as hotato-build.
resource "google_service_account" "rebuild_scheduler" {
  count        = local.maintain
  account_id   = "hotato-rebuild-scheduler"
  display_name = "starts the weekly image rebuild"
  depends_on   = [google_project_service.api]
}

resource "google_project_iam_member" "rebuild_scheduler_builds" {
  count   = local.maintain
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${google_service_account.rebuild_scheduler[0].email}"
}

resource "google_service_account_iam_member" "rebuild_scheduler_acts_as_build" {
  count              = local.maintain
  service_account_id = google_service_account.build.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.rebuild_scheduler[0].email}"
}

# ---- Triggers (1st-gen GitHub App connection) ----
resource "google_cloudbuild_trigger" "infra_apply" {
  count           = local.maintain
  name            = "hotato-infra-apply"
  location        = "global"
  description     = "On push to main touching infra: fmt, validate, plan, apply."
  filename        = "autopilot/build/cloudbuild-infra.yaml"
  included_files  = local.infra_files
  service_account = google_service_account.infra[0].id
  github {
    owner = local.gh_owner
    name  = local.gh_name
    push {
      branch = "^main$"
    }
  }
  substitutions = {
    _APPLY   = "true"
    _TFSTATE = google_storage_bucket.tfstate.name
  }
  depends_on = [google_project_iam_member.infra_owner]
}

resource "google_cloudbuild_trigger" "infra_plan" {
  count           = local.maintain
  name            = "hotato-infra-plan"
  location        = "global"
  description     = "On pull requests touching infra: fmt, validate, plan (no apply)."
  filename        = "autopilot/build/cloudbuild-infra.yaml"
  included_files  = local.infra_files
  service_account = google_service_account.infra_plan[0].id
  github {
    owner = local.gh_owner
    name  = local.gh_name
    pull_request {
      branch          = "^main$"
      comment_control = "COMMENTS_ENABLED_FOR_EXTERNAL_CONTRIBUTORS_ONLY"
    }
  }
  substitutions = {
    _APPLY   = "false"
    _TFSTATE = google_storage_bucket.tfstate.name
  }
  depends_on = [google_project_iam_member.infra_plan, google_storage_bucket_iam_member.infra_plan_read]
}

# Started only by the weekly schedule (schedule.tf): builds main with autopilot/build/cloudbuild.yaml.
resource "google_cloudbuild_trigger" "weekly_rebuild" {
  count           = local.maintain
  name            = "hotato-weekly-rebuild"
  location        = "global"
  description     = "Weekly rebuild of the job and monitor images from main (fresh base images), gates first."
  service_account = google_service_account.build.id
  source_to_build {
    uri       = local.gh_url
    ref       = "refs/heads/main"
    repo_type = "GITHUB"
  }
  git_file_source {
    path      = "autopilot/build/cloudbuild.yaml"
    uri       = local.gh_url
    revision  = "refs/heads/main"
    repo_type = "GITHUB"
  }
  substitutions = {
    _DEPLOY        = "true"
    _CF_ACCOUNT_ID = var.cloudflare_account_id
  }
  lifecycle {
    precondition {
      condition     = var.cloudflare_account_id != ""
      error_message = "Set cloudflare_account_id in hotato.tfvars before self_maintain = true (the rebuild also redeploys the site)."
    }
  }
}
