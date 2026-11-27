resource "google_workflows_workflow" "run_jobs" {
  name            = "hotato-run-jobs"
  region          = var.region
  service_account = google_service_account.workflow.id
  source_contents = file("${path.module}/workflow.yaml")
  depends_on      = [google_project_service.api]
}

locals {
  schedules = {
    atlas         = { cron = "0 * * * *", jobs = ["atlas"] }
    atlas-recheck = { cron = "30 3 * * *", jobs = ["atlas-recheck"] }
    atlas-audit   = { cron = "30 4 * * 3", jobs = ["atlas-audit"] }
    atlas-watch   = { cron = "40 1,7,13,19 * * *", jobs = ["atlas-watch"] }
    atlas-wheel   = { cron = "15 9 * * *", jobs = ["atlas-wheel"] }
    upkeep        = { cron = "0 2 * * *", jobs = ["upkeep"] }
    freshness     = { cron = "0 3 * * *", jobs = ["freshness"] }
    builder       = { cron = "0 4 * * *", jobs = ["builder", "reviewer"] }
    simulate      = { cron = "0 5 * * *", jobs = ["simulate"] }
    scorecard     = { cron = "0 6 * * 1", jobs = ["scorecard"] }
  }

  # Schedules that did not exist before this release are created paused, so they start only
  # when the owner resumes them (RUNBOOK: Atlas). Terraform never flips paused afterwards.
  paused_at_create = toset(["atlas-recheck", "atlas-audit", "atlas-watch", "atlas-wheel"])

  # Interval of each schedule in hours, read from its cron: "M * * * *" hourly, "M H1,H2,.. * * *"
  # every 24/n hours, "M H * * *" daily, anything with a day field set counts as weekly. Used by
  # the heartbeat alerts in monitoring.tf.
  schedule_interval_h = {
    for k, s in local.schedules : k => (
      split(" ", s.cron)[1] == "*" ? 1 : (
        split(" ", s.cron)[2] == "*" && split(" ", s.cron)[4] == "*" ? floor(24 / length(split(",", split(" ", s.cron)[1]))) : 168
      )
    )
  }
  scheduled_jobs = toset(flatten([for s in values(local.schedules) : s.jobs]))

  # Every scheduled job that exists, with the shortest interval it runs at.
  heartbeat_interval_h = {
    for j in setintersection(local.scheduled_jobs, toset(keys(local.jobs))) :
    j => min([for k, s in local.schedules : local.schedule_interval_h[k] if contains(s.jobs, j)]...)
  }
}

# Warn at plan time when a job exists in local.jobs (main.tf) but no schedule runs it:
# it would never run and has no heartbeat alert.
check "every_job_scheduled" {
  assert {
    condition     = length(setsubtract(toset(keys(local.jobs)), local.scheduled_jobs)) == 0
    error_message = "Jobs with no schedule in schedule.tf: ${join(", ", setsubtract(toset(keys(local.jobs)), local.scheduled_jobs))}."
  }
}

resource "google_cloud_scheduler_job" "run" {
  for_each  = local.schedules
  name      = "hotato-${each.key}"
  region    = var.region
  schedule  = each.value.cron
  time_zone = "Etc/UTC"
  paused    = contains(local.paused_at_create, each.key)

  http_target {
    http_method = "POST"
    uri         = "https://workflowexecutions.googleapis.com/v1/projects/${var.project_id}/locations/${var.region}/workflows/${google_workflows_workflow.run_jobs.name}/executions"
    headers     = { "Content-Type" = "application/json" }
    body        = base64encode(jsonencode({ argument = jsonencode({ jobs = each.value.jobs }) }))
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }
  lifecycle {
    ignore_changes = [paused] # the owner resumes and pauses schedules; Terraform only sets the start
  }
  depends_on = [google_project_service.api]
}

# Weekly rebuild of the job and monitor images from main, so base-image and dependency security
# patches land with no person. Runs the full pipeline (tests and gates first) and deploys the new
# images; the jobs keep their DRY_RUN setting. Exists only when self_maintain = true (maintenance.tf).
resource "google_cloud_scheduler_job" "weekly_rebuild" {
  count     = var.self_maintain ? 1 : 0
  name      = "hotato-weekly-rebuild"
  region    = var.region
  schedule  = "0 8 * * 0"
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://cloudbuild.googleapis.com/v1/projects/${var.project_id}/locations/global/triggers/${google_cloudbuild_trigger.weekly_rebuild[0].trigger_id}:run"
    headers     = { "Content-Type" = "application/json" }
    body        = base64encode(jsonencode({ branchName = "main" }))
    oauth_token {
      service_account_email = google_service_account.rebuild_scheduler[0].email
    }
  }
  depends_on = [google_project_service.api]
}
