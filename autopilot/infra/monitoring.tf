# Two notification channels:
# - owner: email to alert_email, only for what the owner must act on now (site or monitor down,
#   budget kill-switch error, Atlas publish failed).
# - lanes: a Pub/Sub topic the fleet pulls (subscription below), for everything else.
# Every policy notifies once, when it opens: no RESOLVED notifications.
resource "google_monitoring_notification_channel" "email" {
  display_name = "hotato alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [google_project_service.api]
}

resource "google_pubsub_topic" "lane_alerts" {
  name       = "hotato-lane-alerts"
  depends_on = [google_project_service.api]
}

resource "google_monitoring_notification_channel" "lanes" {
  display_name = "hotato lane alerts (Pub/Sub)"
  type         = "pubsub"
  labels       = { topic = google_pubsub_topic.lane_alerts.id }
  depends_on   = [google_project_service.api]
}

data "google_project" "this" {
  project_id = var.project_id
}

# Cloud Monitoring publishes as its notification service agent, which it creates with the first
# Pub/Sub channel in the project; hence the depends_on.
resource "google_pubsub_topic_iam_member" "lane_alerts_monitoring" {
  topic      = google_pubsub_topic.lane_alerts.name
  role       = "roles/pubsub.publisher"
  member     = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
  depends_on = [google_monitoring_notification_channel.lanes]
}

# The fleet pulls this subscription (pull only, no push endpoint). Unread notifications are kept
# 7 days; the subscription itself never expires, even when nobody pulls for a while.
resource "google_pubsub_subscription" "lane_alerts" {
  name                       = "hotato-lane-alerts-fleet"
  topic                      = google_pubsub_topic.lane_alerts.id
  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"
  retain_acked_messages      = false
  expiration_policy {
    ttl = ""
  }
}

# Who may pull it: var.lane_alert_readers, set in hotato.tfvars (kept out of this repository).
resource "google_pubsub_subscription_iam_member" "lane_alerts_readers" {
  for_each     = toset(var.lane_alert_readers)
  subscription = google_pubsub_subscription.lane_alerts.name
  role         = "roles/pubsub.subscriber"
  member       = each.value
}

locals {
  uptime_targets = {
    site = { host = trimprefix(var.site_url, "https://"), path = "/" }
    # /robots.txt is the one route `hotato serve` answers without a token and with no workspace data;
    # /health is token-gated and would read as down.
    monitor = { host = trimprefix(google_cloud_run_v2_service.monitor.uri, "https://"), path = "/robots.txt" }
  }
  uptime_subject = {
    site    = "Hotato: ${trimprefix(var.site_url, "https://")} is down"
    monitor = "Hotato: the hosted monitor is down"
  }
  notify_owner = [google_monitoring_notification_channel.email.id]
  notify_lanes = [google_monitoring_notification_channel.lanes.id]

  # hotato_alert kinds that mean the Atlas pages did not reach hotato.dev. At ERROR they raise
  # atlas_publish_failed (owner email), so job_alert leaves them out.
  atlas_publish_kinds = "^atlas_(guard_shrink|guard_structure|publish_failed|live_mismatch|db_unusable|collector_rolled_back)$"
  # hotato_alert kinds that are always followed by a failed Cloud Run execution (jobs/common.py
  # run_job: job_crash re-raises, model_unavailable exits 1), so job_failed already reports them.
  job_failure_kinds = "^(job_crash|model_unavailable)$"
}

resource "google_monitoring_uptime_check_config" "check" {
  for_each     = local.uptime_targets
  display_name = "hotato ${each.key}"
  timeout      = "10s"
  period       = "300s"

  http_check {
    path           = each.value.path
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = each.value.host
    }
  }
}

resource "google_monitoring_alert_policy" "down" {
  for_each     = google_monitoring_uptime_check_config.check
  display_name = "hotato ${each.key} down"
  combiner     = "OR"

  conditions {
    display_name = "uptime check failing"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id=\"${each.value.uptime_check_id}\" AND resource.type=\"uptime_url\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
    }
  }
  alert_strategy {
    notification_prompts = ["OPENED"]
  }
  documentation {
    subject   = local.uptime_subject[each.key]
    content   = "The uptime check for https://${local.uptime_targets[each.key].host}${local.uptime_targets[each.key].path} has failed from more than one region for 5 minutes. No notice is sent when it recovers."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify_owner
}

# ---- Silent-failure alerts. All go to the lanes, except the kill switch and Atlas publish. ----

# (a) A Cloud Run job execution failed (non-zero exit, timeout, crash) for any hotato-* job.
# The one notice for a failed run: workflow_failed and job_alert leave this case out.
resource "google_monitoring_alert_policy" "job_failed" {
  display_name = "hotato job execution failed"
  combiner     = "OR"
  conditions {
    display_name = "Cloud Run job execution failed"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/job/completed_execution_count\" AND resource.type=\"cloud_run_job\" AND resource.label.job_name=starts_with(\"hotato-\") AND metric.label.result=monitoring.regex.full_match(\"(?i).*fail.*\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.job_name"]
      }
      trigger {
        count = 1
      }
    }
  }
  alert_strategy {
    auto_close           = "86400s"
    notification_prompts = ["OPENED"]
  }
  documentation {
    subject   = "Hotato: a run of $${resource.label.job_name} failed"
    content   = "A hotato Cloud Run job execution failed. Read its run record under runs/<job>/ in the state bucket and the job's logs in Cloud Run."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify_lanes
}

# (b1) Cloud Scheduler could not start a run (it logs each failed attempt at ERROR).
resource "google_monitoring_alert_policy" "scheduler_failed" {
  display_name = "hotato scheduler attempt failed"
  combiner     = "OR"
  conditions {
    display_name = "Cloud Scheduler error log"
    condition_matched_log {
      filter           = "resource.type=\"cloud_scheduler_job\" AND resource.labels.job_id=~\"^hotato-\" AND severity>=ERROR"
      label_extractors = { schedule = "EXTRACT(resource.labels.job_id)" }
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close           = "86400s"
    notification_prompts = ["OPENED"]
  }
  documentation {
    subject   = "Hotato: Cloud Scheduler could not start a run"
    content   = "Cloud Scheduler failed to start the hotato-run-jobs workflow. The schedule label names which schedule; its log entry in Cloud Logging says why."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify_lanes
}

# (b2) The runner workflow failed while no hotato job execution failed: a job could not be
# started at all (missing job, permission, quota). When a job fails, the workflow fails right
# after it and job_failed reports that, so that case is excluded here. The 1 h job window covers
# the 30 min workflow window plus metric delay; the 5 min duration lets a late job point arrive.
resource "google_monitoring_alert_policy" "workflow_failed" {
  display_name = "hotato workflow execution failed"
  combiner     = "OR"
  conditions {
    display_name = "Workflows execution failed with no failed job"
    condition_prometheus_query_language {
      query               = "(sum(increase(workflows_googleapis_com:finished_execution_count{monitored_resource=\"workflows.googleapis.com/Workflow\",workflow_id=\"${google_workflows_workflow.run_jobs.name}\",status=~\"(?i).*fail.*\"}[30m])) > 0) unless on() (sum(increase(run_googleapis_com:job_completed_execution_count{monitored_resource=\"cloud_run_job\",job_name=~\"hotato-.*\",result=~\"(?i).*fail.*\"}[1h])) > 0)"
      evaluation_interval = "60s"
      duration            = "300s"
    }
  }
  alert_strategy {
    notification_prompts = ["OPENED"]
  }
  documentation {
    subject   = "Hotato: the run workflow failed before a job ran"
    content   = "A hotato-run-jobs workflow execution failed and no hotato job execution failed with it, so a job could not be started. Read the failed execution in Workflows."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify_lanes
}

# (b3) The budget kill-switch function returned an error or crashed (it runs as a Cloud Run service).
resource "google_monitoring_alert_policy" "killswitch_error" {
  display_name = "hotato budget kill-switch error"
  combiner     = "OR"
  conditions {
    display_name = "kill-switch function error log"
    condition_matched_log {
      filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${google_cloudfunctions2_function.killswitch.name}\" AND severity>=ERROR"
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close           = "86400s"
    notification_prompts = ["OPENED"]
  }
  documentation {
    subject   = "Hotato: the budget kill switch failed, spend may not stop at the cap"
    content   = "The budget kill switch failed, so spend may not stop at the cap. Stop the jobs by hand with the kill command in RUNBOOK.md until it is fixed."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify_owner
}

# (b4) Any Cloud Build build failed: the weekly rebuild, the infra apply, or a manual build.
resource "google_monitoring_alert_policy" "build_failed" {
  display_name = "hotato build failed"
  combiner     = "OR"
  conditions {
    display_name = "Cloud Build failure line"
    condition_matched_log {
      filter           = "resource.type=\"build\" AND (textPayload=\"ERROR\" OR textPayload=~\"^ERROR: build step\")"
      label_extractors = { build = "EXTRACT(resource.labels.build_id)" }
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close           = "86400s"
    notification_prompts = ["OPENED"]
  }
  documentation {
    subject   = "Hotato: a Cloud Build build failed"
    content   = "A Cloud Build build failed (weekly rebuild, infra apply or a manual build). The build label is its id; its log in Cloud Build shows the failed step."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify_lanes
}

# (c) The jobs' alert contract (RUNBOOK.md): a log line with jsonPayload.hotato_alert at WARNING or above.
# Leaves out the kinds another policy already reports for the same event (locals above).
resource "google_monitoring_alert_policy" "job_alert" {
  display_name = "hotato job raised an alert"
  combiner     = "OR"
  conditions {
    display_name = "hotato_alert log line"
    condition_matched_log {
      filter = "jsonPayload.hotato_alert:* AND severity>=WARNING AND NOT jsonPayload.hotato_alert=~\"${local.job_failure_kinds}\" AND NOT (jsonPayload.hotato_alert=~\"${local.atlas_publish_kinds}\" AND severity>=ERROR)"
      label_extractors = {
        kind   = "EXTRACT(jsonPayload.hotato_alert)"
        job    = "EXTRACT(jsonPayload.job)"
        detail = "EXTRACT(jsonPayload.detail)"
      }
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close           = "86400s"
    notification_prompts = ["OPENED"]
  }
  documentation {
    subject   = "Hotato: a job reported a condition that needs the owner"
    content   = "A hotato job reported a condition that needs the owner. The incident labels carry the kind, the job and the detail."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify_lanes
}

# (d) Dead-man's switch: each job logs {"hotato_heartbeat": "<job>"} once per successful run.
resource "google_logging_metric" "heartbeat" {
  name   = "hotato_heartbeat"
  filter = "resource.type=\"cloud_run_job\" AND jsonPayload.hotato_heartbeat:*"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "hotato_job"
      value_type  = "STRING"
      description = "Job name from the heartbeat line."
    }
  }
  label_extractors = { hotato_job = "EXTRACT(jsonPayload.hotato_heartbeat)" }
}

locals {
  # Window = 2x the job's schedule interval (local.heartbeat_interval_h in schedule.tf).
  # atlas-publish is logged by the hourly Atlas job only when hotato.dev/atlas serves its render
  # (jobs/atlas_job.py), so it goes quiet when publishing breaks while the job itself still runs.
  heartbeat_window_h = merge(
    { for j, h in local.heartbeat_interval_h : j => 2 * h },
    var.atlas_site_repo != "" ? { "atlas-publish" = 6 } : {},
  )
  # A plain metric-absence condition allows at most 23.5 h, and PromQL over a log-based metric at most 25 h.
  # Longer windows therefore watch the built-in Cloud Run metric for a succeeded execution instead of the
  # heartbeat log line. The owner's live check still reads every job's heartbeat file.
  absence_max_h = 23.5

  # No alert for a job that has never run (bring-up):
  # - short windows: a metric-absence condition is met only after the series has had data since
  #   the policy was made or changed, so a job with no heartbeat yet never fires.
  # - long windows: "no success in W unless no success in 2W" fires once a job that succeeded
  #   within 2W has gone W without one, and stays quiet for a job with no success at all. The
  #   incident closes at 2W, so a job that stays dead notifies once.
  # Google's minimum PromQL evaluation interval for the 2W lookback: up to 8 days 5 min,
  # up to 16 days 1 h, up to 32 days 2 h, longer 6 h.
  heartbeat_eval_s = {
    for j, w in local.heartbeat_window_h : j => (
      2 * w <= 192 ? 300 : (2 * w <= 384 ? 3600 : (2 * w <= 768 ? 7200 : 21600))
    )
  }
  heartbeat_subject = {
    for j, w in local.heartbeat_window_h : j => (
      j == "atlas-publish"
      ? "Hotato: hotato.dev/atlas has not been published in ${w} h"
      : "Hotato: the ${j} job has not finished a run in ${w} h"
    )
  }
}

resource "google_monitoring_alert_policy" "heartbeat_missing" {
  for_each     = local.heartbeat_window_h
  display_name = "hotato ${each.key} heartbeat missing"
  combiner     = "OR"

  dynamic "conditions" {
    for_each = each.value <= local.absence_max_h ? [1] : []
    content {
      display_name = "no ${each.key} heartbeat for ${each.value} h"
      condition_absent {
        filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.heartbeat.name}\" AND resource.type=\"cloud_run_job\" AND metric.label.hotato_job=\"${each.key}\""
        duration = "${each.value * 3600}s"
        aggregations {
          alignment_period     = "300s"
          per_series_aligner   = "ALIGN_SUM"
          cross_series_reducer = "REDUCE_SUM"
        }
        trigger {
          count = 1
        }
      }
    }
  }

  dynamic "conditions" {
    for_each = each.value > local.absence_max_h ? [1] : []
    content {
      display_name = "no successful ${each.key} run for ${each.value} h"
      condition_prometheus_query_language {
        query               = "absent_over_time(run_googleapis_com:job_completed_execution_count{monitored_resource=\"cloud_run_job\",job_name=\"hotato-${each.key}\",result=\"succeeded\"}[${each.value}h]) unless absent_over_time(run_googleapis_com:job_completed_execution_count{monitored_resource=\"cloud_run_job\",job_name=\"hotato-${each.key}\",result=\"succeeded\"}[${2 * each.value}h])"
        evaluation_interval = "${local.heartbeat_eval_s[each.key]}s"
        duration            = "0s"
      }
    }
  }

  alert_strategy {
    auto_close           = "86400s"
    notification_prompts = ["OPENED"]
  }
  documentation {
    subject   = local.heartbeat_subject[each.key]
    content   = "The ${each.key} job has had no clean run for ${each.value} hours: it did not run, or it ran and did not finish cleanly. Check its schedule, the kill flag, and runs/${each.key}/ in the state bucket."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify_lanes
}

# The Atlas pages did not reach hotato.dev: a guard refusal, a failed deploy, or a live feed that
# is not the render. Separate from job_alert so the notification names the runbook section.
resource "google_monitoring_alert_policy" "atlas_publish_failed" {
  display_name = "hotato Atlas publish failed"
  combiner     = "OR"
  conditions {
    display_name = "Atlas publish refused or failed"
    condition_matched_log {
      filter = "jsonPayload.hotato_alert=~\"${local.atlas_publish_kinds}\" AND severity>=ERROR"
      label_extractors = {
        kind   = "EXTRACT(jsonPayload.hotato_alert)"
        detail = "EXTRACT(jsonPayload.detail)"
      }
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close           = "86400s"
    notification_prompts = ["OPENED"]
  }
  documentation {
    subject   = "Hotato: the Atlas pages were not published to hotato.dev"
    content   = "hotato.dev/atlas was not updated. The kind says why; RUNBOOK.md section Atlas has the step for each kind. The live pages keep the last good render meanwhile."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify_owner
}
