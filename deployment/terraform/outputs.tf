output "service_url" {
  description = "URL of the deployed ContentForge Cloud Run service."
  value       = google_cloud_run_v2_service.agent.uri
}

output "service_account_email" {
  description = "Runtime service account. Grant this identity access to any additional resource the agent must reach."
  value       = google_service_account.agent_runtime.email
}

output "artifact_registry_repository" {
  description = "Docker repository to push agent images to."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agent_images.repository_id}"
}

output "search_datastore_name" {
  description = "Set as CONTENTFORGE_VERTEX_SEARCH_DATASTORE to enable Vertex AI Search retrieval."
  value       = google_discovery_engine_data_store.brand_kb.name
}

output "corpus_bucket" {
  description = "Bucket holding the brand corpus staged for datastore import."
  value       = google_storage_bucket.corpus.name
}

output "sql_connection_name" {
  description = "Cloud SQL connection name for the session store."
  value       = google_sql_database_instance.sessions.connection_name
}

output "secret_references" {
  description = "Secret Manager references injected into the service. Values are populated out of band."
  value = {
    cms_api_token = "${google_secret_manager_secret.cms_api_token.id}/versions/latest"
    db_password   = "${google_secret_manager_secret.db_password.id}/versions/latest"
  }
}

output "secret_ids" {
  description = "Short secret ids, used by bootstrap.sh to add secret versions."
  value = {
    cms_api_token = google_secret_manager_secret.cms_api_token.secret_id
    db_password   = google_secret_manager_secret.db_password.secret_id
  }
}

output "deployer_service_account" {
  description = <<-EOT
    CI deployer identity, for the GCP_SERVICE_ACCOUNT repository variable. This
    is deliberately NOT the runtime service account: the deployer can ship a
    revision but cannot read secrets or the session database. Empty when
    github_repository is unset.
  EOT
  value       = try(google_service_account.deployer[0].email, "")
}

output "workload_identity_provider" {
  description = <<-EOT
    Full resource name of the GitHub OIDC provider, for the
    GCP_WORKLOAD_IDENTITY_PROVIDER repository variable. Empty when
    github_repository is unset.
  EOT
  value       = try(google_iam_workload_identity_pool_provider.github[0].name, "")
}

output "post_apply_next_steps" {
  description = "What to do after the first apply."
  value       = <<-EOT
    1. Populate secret values and create the database user (never in Terraform,
       which would persist them in state):
         printf '%s' "$CMS_TOKEN" | gcloud secrets versions add ${google_secret_manager_secret.cms_api_token.secret_id} --data-file=-
         printf '%s' "$DB_PASSWORD" | gcloud secrets versions add ${google_secret_manager_secret.db_password.secret_id} --data-file=-
         gcloud sql users create contentforge_agent \
           --instance=${google_sql_database_instance.sessions.name} --password="$DB_PASSWORD"
    2. Stage and import the brand corpus:
         gsutil -m cp content_forge/memory/corpus/*.json gs://${google_storage_bucket.corpus.name}/
    3. Build and deploy the image:
         gcloud builds submit --config deployment/cloudbuild.yaml
    4. Grant editorial staff invoke access via the invoker_members variable.
  EOT
}
