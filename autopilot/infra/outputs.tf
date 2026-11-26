output "state_bucket" {
  value = google_storage_bucket.state.name
}

output "build_service_account" {
  value       = google_service_account.build.email
  description = "Add this as the PyPI trusted publisher (Google Cloud service account)."
}

output "github_workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "github_deployer_service_account" {
  value = google_service_account.deployer.email
}

output "jobs_image" {
  value = local.jobs_image
}

output "kill_command" {
  value = "printf stop | gcloud storage cp - gs://${google_storage_bucket.state.name}/flags/kill"
}
