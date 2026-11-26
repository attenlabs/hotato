# The budget topic `budget-alerts` and the budgets are created by the owner's one-time setup script.
# Here: the kill-switch function that listens on that topic (Eventarc makes the subscription).
data "google_pubsub_topic" "budget_alerts" {
  name = "budget-alerts"
}

data "archive_file" "killswitch" {
  type        = "zip"
  source_dir  = "${path.module}/function"
  output_path = "${path.module}/.killswitch.zip"
}

resource "google_storage_bucket_object" "killswitch_src" {
  name   = "src/killswitch-${data.archive_file.killswitch.output_md5}.zip"
  bucket = google_storage_bucket.state.name
  source = data.archive_file.killswitch.output_path
}

resource "google_cloudfunctions2_function" "killswitch" {
  name     = "hotato-killswitch"
  location = var.region
  build_config {
    runtime         = "python312"
    entry_point     = "on_budget"
    service_account = google_service_account.function_build.id
    source {
      storage_source {
        bucket = google_storage_bucket.state.name
        object = google_storage_bucket_object.killswitch_src.name
      }
    }
  }
  service_config {
    available_memory      = "256M"
    max_instance_count    = 1
    service_account_email = google_service_account.killswitch.email
    environment_variables = {
      STATE_BUCKET = google_storage_bucket.state.name
      PHASE        = var.phase
    }
  }
  event_trigger {
    trigger_region        = var.region
    event_type            = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic          = data.google_pubsub_topic.budget_alerts.id
    retry_policy          = "RETRY_POLICY_DO_NOT_RETRY"
    service_account_email = google_service_account.killswitch.email
  }
  depends_on = [google_project_service.api, google_project_iam_member.function_build_builder]
}

resource "google_cloud_run_v2_service_iam_member" "killswitch_invoker" {
  name     = google_cloudfunctions2_function.killswitch.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.killswitch.email}"
}

# Builds the function. The organization disables the default compute account's automatic grants, so the
# build gets its own account with the one role a function build needs.
resource "google_service_account" "function_build" {
  account_id   = "hotato-function-build"
  display_name = "Builds the budget kill-switch function"
  depends_on   = [google_project_service.api]
}

resource "google_project_iam_member" "function_build_builder" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = "serviceAccount:${google_service_account.function_build.email}"
}
