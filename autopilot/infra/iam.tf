# One service account per job, each with only what it needs.
resource "google_service_account" "job" {
  for_each     = local.jobs
  account_id   = "hotato-job-${each.key}"
  display_name = "hotato ${each.key} job"
  depends_on   = [google_project_service.api]
}

resource "google_service_account" "monitor" {
  account_id   = "hotato-monitor"
  display_name = "hotato hosted monitoring service"
  depends_on   = [google_project_service.api]
}

resource "google_service_account" "workflow" {
  account_id   = "hotato-workflow"
  display_name = "runs the Cloud Run jobs"
  depends_on   = [google_project_service.api]
}

resource "google_service_account" "scheduler" {
  account_id   = "hotato-scheduler"
  display_name = "starts workflow executions"
  depends_on   = [google_project_service.api]
}

resource "google_service_account" "killswitch" {
  account_id   = "hotato-killswitch"
  display_name = "budget kill-switch function"
  depends_on   = [google_project_service.api]
}

resource "google_service_account" "build" {
  account_id   = "hotato-build"
  display_name = "Cloud Build pipeline (also the PyPI trusted publisher identity)"
  depends_on   = [google_project_service.api]
}

resource "google_service_account" "deployer" {
  account_id   = "hotato-github-deployer"
  display_name = "GitHub workflow: starts builds, reads run records"
  depends_on   = [google_project_service.api]
}

# State bucket access: every job reads and writes its own state and run records.
resource "google_storage_bucket_iam_member" "job_state" {
  for_each = local.jobs
  bucket   = google_storage_bucket.state.name
  role     = "roles/storage.objectUser"
  member   = "serviceAccount:${google_service_account.job[each.key].email}"
}

resource "google_storage_bucket_iam_member" "monitor_state" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.monitor.email}"
}

resource "google_storage_bucket_iam_member" "killswitch_state" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.killswitch.email}"
}

resource "google_storage_bucket_iam_member" "build_state" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.build.email}"
}

resource "google_storage_bucket_iam_member" "deployer_read" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.deployer.email}"
}

# Vertex AI only for jobs that call models.
resource "google_project_iam_member" "vertex" {
  for_each = { for k, v in local.jobs : k => v if v.vertex }
  project  = var.project_id
  role     = "roles/aiplatform.user"
  member   = "serviceAccount:${google_service_account.job[each.key].email}"
}

# Secrets: each job reads only its own secrets.
locals {
  job_secret_pairs = merge([
    for j, v in local.jobs : { for s in v.secrets : "${j}/${s}" => { job = j, secret = s } }
  ]...)
}

# Optional secrets (GitHub App): read at run time when present; jobs fall back to the bot token.
locals {
  job_optional_secret_pairs = merge([
    for j, v in local.jobs : { for s in try(v.optional_secrets, []) : "${j}/${s}" => { job = j, secret = s } }
  ]...)
}

resource "google_secret_manager_secret_iam_member" "job_optional" {
  for_each  = local.job_optional_secret_pairs
  secret_id = local.secret_ids[each.value.secret]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.job[each.value.job].email}"
}

resource "google_secret_manager_secret_iam_member" "job" {
  for_each  = local.job_secret_pairs
  secret_id = local.secret_ids[each.value.secret]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.job[each.value.job].email}"
}

# Workflow SA may run only the hotato jobs; scheduler SA may only start the workflow.
resource "google_cloud_run_v2_job_iam_member" "workflow_runs" {
  for_each = local.jobs
  name     = google_cloud_run_v2_job.job[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_project_iam_member" "workflow_runs_ops" {
  project = var.project_id
  role    = "roles/run.viewer"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_project_iam_member" "scheduler_workflows" {
  project = var.project_id
  role    = "roles/workflows.invoker"
  member  = "serviceAccount:${google_service_account.scheduler.email}"
}

# Cloud Build pipeline: build images, deploy to Cloud Run and Firebase Hosting, write logs.
resource "google_project_iam_member" "build" {
  for_each = toset([
    "roles/logging.logWriter", "roles/run.developer",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.build.email}"
}

# Artifact Registry, per repository: the build pushes images to the image repository and only reads
# the private Python repository, so a build can never replace a private package.
resource "google_artifact_registry_repository_iam_member" "build_images" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.build.email}"
}

resource "google_artifact_registry_repository_iam_member" "build_private_py" {
  location   = google_artifact_registry_repository.private_py.location
  repository = google_artifact_registry_repository.private_py.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.build.email}"
}

resource "google_service_account_iam_member" "build_acts_as" {
  for_each           = merge({ for k, v in google_service_account.job : k => v.name }, { monitor = google_service_account.monitor.name })
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.build.email}"
}

# Workload identity federation for GitHub: no keys. Only the named repo can act as the deployer.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub"
  depends_on                = [google_project_service.api]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == \"${var.github_repo}\""
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

resource "google_project_iam_member" "deployer_builds" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_acts_as_build" {
  service_account_id = google_service_account.build.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# The build pipeline reads the Cloudflare Pages token to deploy hotato.dev.
resource "google_secret_manager_secret_iam_member" "build_pages" {
  secret_id = local.secret_ids["cloudflare-pages-token"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.build.email}"
}

resource "google_project_iam_member" "killswitch_event_receiver" {
  project = var.project_id
  role    = "roles/eventarc.eventReceiver"
  member  = "serviceAccount:${google_service_account.killswitch.email}"
}

# `gcloud builds submit` uploads the source to <project>_cloudbuild; a build running as hotato-build must read it.
resource "google_storage_bucket_iam_member" "build_source" {
  bucket = "${var.project_id}_cloudbuild"
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.build.email}"
}

# The Atlas jobs install the collector version the hourly job settled on from the private Python
# repository at run time (jobs/atlas_collector.py): read only. atlas-wheel is the one identity
# that uploads new versions (jobs/atlas_wheel.py); Artifact Registry never replaces a version.
resource "google_artifact_registry_repository_iam_member" "atlas_private_py_read" {
  for_each   = toset(["atlas", "atlas-recheck", "atlas-audit"])
  location   = google_artifact_registry_repository.private_py.location
  repository = google_artifact_registry_repository.private_py.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.job[each.key].email}"
}

resource "google_artifact_registry_repository_iam_member" "atlas_wheel_write" {
  location   = google_artifact_registry_repository.private_py.location
  repository = google_artifact_registry_repository.private_py.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.job["atlas-wheel"].email}"
}

# atlas-watch may start the hourly Atlas job (no overrides) when the live pages are stale.
resource "google_cloud_run_v2_job_iam_member" "atlas_watch_runs_atlas" {
  name     = google_cloud_run_v2_job.job["atlas"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.job["atlas-watch"].email}"
}
