resource "google_monitoring_notification_channel" "email" {
  display_name = "hotato alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [google_project_service.api]
}

locals {
  uptime_targets = {
    site = { host = trimprefix(var.site_url, "https://"), path = "/" }
    # /robots.txt is the one route `hotato serve` answers without a token and with no workspace data;
    # /health is token-gated and would read as down.
    monitor = { host = trimprefix(google_cloud_run_v2_service.monitor.uri, "https://"), path = "/robots.txt" }
  }
  notify = [google_monitoring_notification_channel.email.id]
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
  notification_channels = local.notify
}

# ---- Silent-failure alerts. Every one emails the same channel. ----

# (a) A Cloud Run job execution failed (non-zero exit, timeout, crash) for any hotato-* job.
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
    auto_close = "86400s"
  }
  documentation {
    content   = "A hotato Cloud Run job execution failed. Read its run record under runs/<job>/ in the state bucket and the job's logs in Cloud Run."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify
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
    auto_close = "86400s"
  }
  notification_channels = local.notify
}

# (b2) A Workflows execution (the runner that starts the jobs) ended in a failed state.
resource "google_monitoring_alert_policy" "workflow_failed" {
  display_name = "hotato workflow execution failed"
  combiner     = "OR"
  conditions {
    display_name = "Workflows execution failed"
    condition_threshold {
      filter          = "metric.type=\"workflows.googleapis.com/finished_execution_count\" AND resource.type=\"workflows.googleapis.com/Workflow\" AND resource.label.workflow_id=\"${google_workflows_workflow.run_jobs.name}\" AND metric.label.status=monitoring.regex.full_match(\"(?i).*fail.*\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
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
  alert_strategy {
    auto_close = "86400s"
  }
  notification_channels = local.notify
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
    auto_close = "86400s"
  }
  documentation {
    content   = "The budget kill switch failed, so spend may not stop at the cap. Stop the jobs by hand with the kill command in RUNBOOK.md until it is fixed."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify
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
    auto_close = "86400s"
  }
  notification_channels = local.notify
}

# (c) The jobs' alert contract (RUNBOOK.md): a log line with jsonPayload.hotato_alert at WARNING or above.
resource "google_monitoring_alert_policy" "job_alert" {
  display_name = "hotato job raised an alert"
  combiner     = "OR"
  conditions {
    display_name = "hotato_alert log line"
    condition_matched_log {
      filter = "jsonPayload.hotato_alert:* AND severity>=WARNING"
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
    auto_close = "86400s"
  }
  documentation {
    content   = "A hotato job reported a condition that needs the owner. The incident labels carry the kind, the job and the detail."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify
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
  heartbeat_window_h = { for j, h in local.heartbeat_interval_h : j => 2 * h }
  # A plain metric-absence condition allows at most 23.5 h, and PromQL over a log-based metric at most 25 h.
  # Longer windows therefore watch the built-in Cloud Run metric for a succeeded execution instead of the
  # heartbeat log line (absent_over_time also fires for a job that has never run). The owner's live check
  # still reads every job's heartbeat file.
  absence_max_h = 23.5
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
      display_name = "no ${each.key} heartbeat for ${each.value} h"
      condition_prometheus_query_language {
        query = "absent_over_time(run_googleapis_com:job_completed_execution_count{monitored_resource=\"cloud_run_job\",job_name=\"hotato-${each.key}\",result=\"succeeded\"}[${each.value}h])"
        # Google requires a slower evaluation for long lookbacks (at least 1 h for 14 days).
        evaluation_interval = each.value > 48 ? "3600s" : "300s"
        duration            = "0s"
      }
    }
  }

  alert_strategy {
    auto_close = "86400s"
  }
  documentation {
    content   = "The ${each.key} job has not logged a heartbeat for ${each.value} hours: it did not run, or it ran and did not finish cleanly. Check its schedule, the kill flag, and runs/${each.key}/ in the state bucket."
    mime_type = "text/markdown"
  }
  notification_channels = local.notify
}
