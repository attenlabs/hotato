locals {
  gh_secrets       = ["github-bot-token"]
  existing_secrets = ["github-bot-token", "cloudflare-pages-token"] # created by the owner's one-time setup script
  sim_secrets      = ["telephony-account-sid", "telephony-auth-token", "voice-provider-api-key"]
  # OPTIONAL GitHub App credentials. Terraform makes the empty containers; jobs read them at
  # run time from Secret Manager (never as env refs, so a missing version breaks nothing) and
  # fall back to github-bot-token while they are empty.
  gh_app_secrets = ["github-app-id", "github-app-private-key", "github-app-installation-id"]
  # OPTIONAL read-only token for the private site and collector repositories (contents: read).
  # Empty is fine while the GitHub App or the bot token can read them (jobs/atlas_site.py).
  atlas_repo_secrets = ["atlas-repos-token"]
  # Secret containers Terraform creates (values are added by the owner).
  created_secrets = concat(local.sim_secrets, local.gh_app_secrets, local.atlas_repo_secrets)
  # What the Atlas jobs read at run time (never as env refs, so a missing version breaks nothing):
  # every Atlas job may read the private repositories; only the hourly job deploys the pages.
  atlas_repo_read_secrets = concat(["github-bot-token"], local.gh_app_secrets, local.atlas_repo_secrets)
  atlas_publish_secrets   = concat(local.atlas_repo_read_secrets, ["cloudflare-pages-token"])

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
  # Optional per job: args (container arguments), cpu, memory, live (DRY_RUN=0: the job writes
  # nothing outside this project but the Atlas pages), env (extra variables), cache (mounts the
  # state bucket at /mnt/state; the collector's fetch cache lives in atlas/cache/, kept 30 days).
  job_specs = {
    atlas         = { script = "atlas_job.py", args = ["run"], timeout = 5400, vertex = true, secrets = [], optional_secrets = local.atlas_publish_secrets, cpu = "2", memory = "4Gi", live = var.atlas_publish_live, cache = true }
    atlas-recheck = { script = "atlas_job.py", args = ["recheck"], timeout = 3600, vertex = false, secrets = [], optional_secrets = local.atlas_repo_read_secrets, live = true, cache = true }
    atlas-audit   = { script = "atlas_job.py", args = ["audit"], timeout = 3600, vertex = true, secrets = [], optional_secrets = local.atlas_repo_read_secrets, live = true, cache = true }
    atlas-watch   = { script = "atlas_watch.py", timeout = 600, vertex = false, secrets = [], optional_secrets = [], live = true }
    atlas-wheel   = { script = "atlas_wheel.py", timeout = 3600, vertex = false, secrets = [], optional_secrets = local.atlas_repo_read_secrets, cpu = "2", memory = "4Gi", live = true }
    upkeep        = { script = "upkeep.py", timeout = 900, vertex = false, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    builder       = { script = "builder.py", timeout = 3600, vertex = true, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    reviewer      = { script = "reviewer.py", timeout = 1800, vertex = true, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    freshness     = { script = "freshness.py", timeout = 1800, vertex = false, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    scorecard     = { script = "scorecard.py", timeout = 900, vertex = false, secrets = local.gh_secrets, optional_secrets = local.gh_app_secrets }
    simulate      = { script = "simulate.py", timeout = 3600, vertex = true, secrets = local.sim_secrets, optional_secrets = [] }
  }
  job_defaults = { args = [], cpu = "1", memory = "2Gi", live = false, cache = false }
  jobs         = { for k, v in local.job_specs : k => merge(local.job_defaults, v) }

  # Settings shared by the Atlas jobs. Repository names and the account id come from tfvars.
  atlas_env = {
    ATLAS_SITE_REPO        = var.atlas_site_repo
    ATLAS_COLLECTOR_REPO   = var.atlas_collector_repo
    ATLAS_SITE_URL         = var.site_url
    ATLAS_PAGES_PROJECT    = "hotato-site"
    ATLAS_PY_REPO          = "hotato-private-py"
    CLOUDFLARE_ACCOUNT_ID  = var.cloudflare_account_id
    ATLAS_DISTILL_BUDGET_S = "2400"
    ATLAS_RUN_MAX_USD      = "5"
    ATLAS_IMPROVE_LIMIT    = "150"
    ATLAS_LOCK_TTL_S       = "6000"
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
      age            = 45
      matches_prefix = ["atlas/snapshots/"]
    }
    action {
      type = "Delete"
    }
  }
  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["atlas/quarantine/", "atlas/cache/"]
    }
    action {
      type = "Delete"
    }
  }
  # Replaced generations of atlas/atlas.db (one per hourly run) are the first line of restore
  # (jobs/atlas_state.py); 14 days of them, like every other replaced object.
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

# Private Python packages installed into the jobs image at build time (the Atlas collector,
# hotato-atlas). Artifact Registry repositories are private: only the IAM below and project
# owners can reach it. The owner uploads versions (build/publish-atlas-wheel.sh); the build only reads.
resource "google_artifact_registry_repository" "private_py" {
  repository_id = "hotato-private-py"
  location      = var.region
  format        = "PYTHON"
  description   = "Private Python packages for the jobs image. Never public."
  depends_on    = [google_project_service.api]
}
