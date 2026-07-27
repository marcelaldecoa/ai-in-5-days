###############################################################################
# Secret Manager
#
# Two rules encoded here:
#
# 1. Terraform creates the secret *container* but never its value. Writing a
#    secret value in Terraform would persist it in plaintext in the state file,
#    which is exactly the leak this file exists to prevent. Values are added out
#    of band (see the note below), so no credential is ever in source control or
#    in state.
#
# 2. Access is granted per-secret to the runtime service account, not
#    project-wide. The agent can read the CMS token and the DB password and
#    nothing else in the project.
#
# Populate values after `terraform apply`:
#
#   printf '%s' "$CMS_TOKEN" | gcloud secrets versions add contentforge-dev-cms-api-token --data-file=-
#   printf '%s' "$DB_PASSWORD" | gcloud secrets versions add contentforge-dev-db-password --data-file=-
###############################################################################

resource "google_secret_manager_secret" "cms_api_token" {
  project   = var.project_id
  secret_id = "contentforge-${var.environment}-cms-api-token"
  labels    = local.common_labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "db_password" {
  project   = var.project_id
  secret_id = "contentforge-${var.environment}-db-password"
  labels    = local.common_labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

# Only needed when using the Gemini Developer API instead of Vertex AI. The
# deployed configuration uses Vertex AI with workload identity, so this secret
# normally stays empty - it exists so a developer running against AI Studio has
# a managed place to put the key rather than an .env file.
resource "google_secret_manager_secret" "gemini_api_key" {
  count = var.create_gemini_api_key_secret ? 1 : 0

  project   = var.project_id
  secret_id = "contentforge-${var.environment}-gemini-api-key"
  labels    = local.common_labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

###############################################################################
# Least-privilege access bindings - per secret, never project-wide
###############################################################################

resource "google_secret_manager_secret_iam_member" "cms_api_token_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.cms_api_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "db_password_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.db_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "gemini_api_key_accessor" {
  count = var.create_gemini_api_key_secret ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.gemini_api_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime.email}"
}
