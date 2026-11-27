variable "auto_promote_jobs" {
  type        = list(string)
  default     = []
  description = "Jobs allowed to promote themselves from shadow to live after 7 clean days. The owner sets this (RUNBOOK step 12); empty keeps every job in shadow."
}

# Hosted monitoring service (scales to zero).
resource "google_cloud_run_v2_service" "monitor" {
  name     = "hotato-monitor"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.monitor.email
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    containers {
      image = local.placeholder_image
      env {
        name  = "STATE_BUCKET"
        value = google_storage_bucket.state.name
      }
      volume_mounts {
        name       = "serve-token"
        mount_path = "/secrets/serve-token"
      }
    }
    volumes {
      name = "serve-token"
      secret {
        secret = google_secret_manager_secret.serve_token.secret_id
        items {
          version = "latest"
          path    = "token"
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image] # Cloud Build deploys the real image
  }
  depends_on = [google_project_service.api, google_secret_manager_secret_iam_member.monitor_serve_token]
}

resource "google_cloud_run_v2_service_iam_member" "monitor_public" {
  count    = var.monitor_public ? 1 : 0
  name     = google_cloud_run_v2_service.monitor.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# One Cloud Run job per task. All use one image; the command picks the entrypoint.
resource "google_cloud_run_v2_job" "job" {
  for_each = local.jobs
  name     = "hotato-${each.key}"
  location = var.region

  template {
    template {
      service_account = google_service_account.job[each.key].email
      # The Atlas jobs retry once: they hold a lock and upload over a generation, so a retry
      # never doubles a write, and a crash on a candidate collector retries on the current one.
      max_retries           = startswith(each.key, "atlas") ? 1 : 0
      timeout               = "${each.value.timeout}s"
      execution_environment = each.value.cache ? "EXECUTION_ENVIRONMENT_GEN2" : null

      dynamic "volumes" {
        for_each = each.value.cache ? [1] : []
        content {
          name = "state"
          gcs {
            bucket    = google_storage_bucket.state.name
            read_only = false
          }
        }
      }

      containers {
        image   = local.placeholder_image
        command = ["python", "jobs/${each.value.script}"]
        args    = length(each.value.args) > 0 ? each.value.args : null
        resources {
          limits = {
            cpu    = each.value.cpu
            memory = each.value.memory
          }
        }
        dynamic "volume_mounts" {
          for_each = each.value.cache ? [1] : []
          content {
            name       = "state"
            mount_path = "/mnt/state"
          }
        }
        dynamic "env" {
          for_each = startswith(each.key, "atlas") ? merge(local.atlas_env, each.value.cache ? { ATLAS_CACHE_DIR = "/mnt/state/atlas/cache" } : {}) : {}
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name  = "STATE_BUCKET"
          value = google_storage_bucket.state.name
        }
        env {
          name  = "GCP_PROJECT"
          value = var.project_id
        }
        env {
          name  = "GCP_REGION"
          value = var.region
        }
        env {
          name  = "HOTATO_REPO"
          value = var.github_repo
        }
        env {
          name  = "PHASE"
          value = var.phase
        }
        # The GitHub and phone jobs write outward, so going live needs the owner's opt-in:
        # jobs listed in var.auto_promote_jobs run "auto" (shadow until 7 clean days, then live, demoted on any
        # gate violation; jobs/promotion.py). Every other job is forced to shadow ("1"), except the
        # Atlas jobs marked live in main.tf: they write only the state bucket, the private Python
        # repository and (hotato-atlas, var.atlas_publish_live) the hotato.dev/atlas pages.
        env {
          name  = "DRY_RUN"
          value = each.value.live ? "0" : (contains(var.auto_promote_jobs, each.key) ? "auto" : "1")
        }
        env {
          name  = "AGENTS_CAP_USD"
          value = tostring(local.caps.agents)
        }
        env {
          name  = "SIM_CAP_USD"
          value = tostring(local.caps.sim)
        }
        env {
          name  = "BUILDER_MAX_USD"
          value = tostring(local.caps.builder_run)
        }
        env {
          name  = "REVIEWER_MAX_USD"
          value = tostring(local.caps.reviewer_run)
        }
        dynamic "env" {
          for_each = var.enable_secrets ? each.value.secrets : []
          content {
            name = upper(replace(env.value, "-", "_"))
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image]
  }
  depends_on = [google_project_service.api]
}
