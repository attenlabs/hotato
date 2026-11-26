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
      max_retries     = 0
      timeout         = "${each.value.timeout}s"

      containers {
        image   = local.placeholder_image
        command = ["python", "jobs/${each.value.script}"]
        resources {
          limits = {
            cpu    = "1"
            memory = "2Gi"
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
        # Every job writes outward (GitHub, phone calls), so going live needs the owner's opt-in:
        # jobs listed in var.auto_promote_jobs run "auto" (shadow until 7 clean days, then live, demoted on any
        # gate violation; jobs/promotion.py). Every other job is forced to shadow ("1").
        env {
          name  = "DRY_RUN"
          value = contains(var.auto_promote_jobs, each.key) ? "auto" : "1"
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
