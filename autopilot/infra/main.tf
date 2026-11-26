locals {
  gh_secrets       = ["github-bot-token"]
  existing_secrets = ["github-bot-token", "cloudflare-pages-token"] # created by the owner's one-time setup script
  sim_secrets      = ["telephony-account-sid", "telephony-auth-token", "voice-provider-api-key"]
  # OPTIONAL GitHub App credentials. Terraform makes the empty containers; jobs read them at
  # run time from Secret Manager (never as env refs, so a missing version breaks nothing) and
  # fall back to github-bot-token while they are empty.
  gh_app_secrets = ["github-app-id", "github-app-private-key", "github-app-installation-id"]
  # Secret containers Terraform creates (values are added by the owner).
  created_secrets = concat(local.sim_secrets, local.gh_app_secrets)

  jobs_image        = "${var.region}-docker.pkg.dev/${var.project_id}/hotato/jobs:latest"
  placeholder_image = "us-docker.pkg.dev/cloudrun/container/hello"

  # Phase limits (USD). Jobs also keep a spend ledger in the state bucket and stop at these caps.
  sprint_caps = {
    agents       = var.sprint_budget_usd.agents
    sim          = var.sprint_budget_usd.simulated_calls
    builder_run  = 25
    reviewer_run = 5
  }
  monthly_caps = {
    agents       = var.monthly_budget_usd.agents
    sim          = var.monthly_budget_usd.simulated_calls
    builder_run  = 8
    reviewer_run = 2
  }
  caps = var.phase == "sprint" ? local.sprint_caps : local.monthly_caps

  # One entry per Cloud Run job. vertex = may call Vertex AI. secrets = Secret Manager names (env = upper snake case).
  # optional_secrets = read by the job itself from Secret Manager at run time; may be empty.
  jobs = {
    atlas     = { script = "atlas_job.py", timeout = 3600, vertex = true, secrets = [], optional_secrets = [] }
    upkeep    = { script = "upkeep.py", timeout = 900, vertex = false, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    builder   = { script = "builder.py", timeout = 3600, vertex = true, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    reviewer  = { script = "reviewer.py", timeout = 1800, vertex = true, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    freshness = { script = "freshness.py", timeout = 1800, vertex = false, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    scorecard = { script = "scorecard.py", timeout = 900, vertex = false, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    simulate  = { script = "simulate.py", timeout = 3600, vertex = true, secrets = local.sim_secrets, optional_secrets = [] }
  }
}

# Project, budgets and base APIs come from the owner's one-time setup script. Only APIs it does not enable are added here.
resource "google_project_service" "api" {
  for_each           = toset(["eventarc.googleapis.com", "workflowexecutions.googleapis.com", "cloudresourcemanager.googleapis.com"])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---- State bucket: Atlas database + cache, run logs, kill-switch flag ----
resource "google_storage_bucket" "state" {
  name                        = "${var.project_id}-state"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning { enabled = true }

  lifecycle_rule {
    condition {
      age            = 365
      matches_prefix = ["runs/"]
    }
    action {
      type = "Delete"
    }
  }
  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["cache/"]
    }
    action {
      type = "Delete"
    }
  }
  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 14
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
  depends_on = [google_project_service.api]
}

# ---- Secret Manager: names only; values are added by the owner ----
data "google_secret_manager_secret" "existing" {
  for_each  = toset(local.existing_secrets)
  secret_id = each.value
}

resource "google_secret_manager_secret" "s" {
  for_each  = toset(local.created_secrets)
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.api]
}

# Bearer token for `hotato serve` (every route but / and /robots.txt is token-gated).
# Terraform makes the name only; the value is added as a version before the first deploy (RUNBOOK step 5a).
resource "google_secret_manager_secret" "serve_token" {
  secret_id = "serve-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.api]
}

resource "google_secret_manager_secret_iam_member" "monitor_serve_token" {
  secret_id = google_secret_manager_secret.serve_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.monitor.email}"
}

locals {
  secret_ids = merge(
    { for k, v in data.google_secret_manager_secret.existing : k => v.id },
    { for k, v in google_secret_manager_secret.s : k => v.id },
  )
}

resource "google_artifact_registry_repository" "images" {
  repository_id = "hotato"
  location      = var.region
  format        = "DOCKER"

  # Bounded forever: keep `latest` and the 10 newest versions of each image; delete untagged
  # versions after 14 days and anything else after 30 days. KEEP rules win over DELETE rules.
  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-latest-tag"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["latest"]
    }
  }
  cleanup_policies {
    id     = "keep-10-newest"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  cleanup_policies {
    id     = "delete-untagged-after-14d"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "1209600s"
    }
  }
  cleanup_policies {
    id     = "delete-after-30d"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "2592000s"
    }
  }
  depends_on = [google_project_service.api]
}
