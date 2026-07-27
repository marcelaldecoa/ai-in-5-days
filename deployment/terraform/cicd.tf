###############################################################################
# Continuous deployment - Workload Identity Federation for GitHub Actions
#
# Why WIF and not a service-account key: a downloaded JSON key is a long-lived
# credential that must be pasted into GitHub secrets, never expires on its own,
# and grants full impersonation to anyone who can read it. WIF instead exchanges
# GitHub's short-lived OIDC token for a short-lived access token, scoped by an
# attribute condition to *this repository only*. There is no key to leak,
# rotate, or accidentally commit.
#
# Why a separate deployer identity: the CI principal needs run.admin and
# artifactregistry.writer to ship a revision. The *agent* must never hold those
# - a runtime that can redeploy itself can rewrite its own guardrails. So the
# deployer and the runtime are two service accounts with disjoint permissions,
# joined only by `roles/iam.serviceAccountUser`, which lets the deployer launch
# a service *as* the runtime identity without inheriting its access.
#
# Created only when `github_repository` is set; otherwise no CI/CD identity
# exists at all.
###############################################################################

locals {
  cicd_enabled = var.github_repository != ""
}

resource "google_iam_workload_identity_pool" "github" {
  count = local.cicd_enabled ? 1 : 0

  project                   = var.project_id
  workload_identity_pool_id = "contentforge-${var.environment}-gh"
  display_name              = "ContentForge GitHub Actions"
  description               = "Keyless deployment identity for ${var.github_repository}"

  depends_on = [google_project_service.required]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = local.cicd_enabled ? 1 : 0

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # The security boundary. Without this condition, ANY GitHub repository on the
  # internet could mint tokens against this pool.
  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

###############################################################################
# Deployer identity - CI only, never the agent at runtime
###############################################################################

resource "google_service_account" "deployer" {
  count = local.cicd_enabled ? 1 : 0

  project      = var.project_id
  account_id   = "contentforge-${var.environment}-deploy"
  display_name = "ContentForge CI deployer (${var.environment})"
  description  = "Impersonated by GitHub Actions via Workload Identity Federation. Deploy-time only; holds no runtime data access."
}

# Only workflows from the configured repository may impersonate the deployer.
resource "google_service_account_iam_member" "github_impersonates_deployer" {
  count = local.cicd_enabled ? 1 : 0

  service_account_id = google_service_account.deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member = join("", [
    "principalSet://iam.googleapis.com/",
    google_iam_workload_identity_pool.github[0].name,
    "/attribute.repository/",
    var.github_repository,
  ])
}

# Deploy-time permissions. Deliberately excludes anything that reads user data:
# no secretmanager.secretAccessor, no cloudsql.client, no discoveryengine access.
# A compromised CI run can ship a bad revision - it cannot read the session
# database or the CMS credential.
resource "google_project_iam_member" "deployer_roles" {
  for_each = local.cicd_enabled ? toset([
    "roles/run.admin",                # deploy Cloud Run revisions
    "roles/aiplatform.user",          # deploy to Agent Engine
    "roles/artifactregistry.writer",  # push container images
    "roles/cloudbuild.builds.editor", # run builds
    "roles/storage.objectAdmin",      # build staging + corpus upload
  ]) : toset([])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer[0].email}"
}

# Lets the deployer launch a service that *runs as* the runtime service account,
# without granting it any of that account's own permissions.
resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  count = local.cicd_enabled ? 1 : 0

  service_account_id = google_service_account.agent_runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer[0].email}"
}
