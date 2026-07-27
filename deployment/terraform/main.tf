###############################################################################
# ContentForge - core infrastructure
#
# Everything the agent needs at runtime is declared here rather than clicked
# together in the console, so an environment can be recreated from scratch and
# reviewed in a diff.
#
#   terraform init
#   terraform apply -var="project_id=my-project" -var="environment=dev"
#
# Provisions:
#   main.tf     - providers, APIs, service account, Artifact Registry, Cloud Run
#   secrets.tf  - Secret Manager secrets and least-privilege accessor bindings
#   search.tf   - Vertex AI Search datastore for the brand knowledge base
#   database.tf - Cloud SQL Postgres for persistent session state
#   variables.tf / outputs.tf
###############################################################################

terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Remote state, so the team shares one source of truth and concurrent applies
  # are locked. Deliberately a *partial* configuration: bucket and prefix are
  # supplied at init time by deployment/bootstrap.sh, which creates the bucket
  # first. That keeps the project id out of version control and lets one
  # configuration serve dev/staging/prod via different prefixes.
  #
  #   terraform init -backend-config="bucket=..." -backend-config="prefix=..."
  #
  # CI validates with `-backend=false` and so never touches remote state.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  service_name = "contentforge-${var.environment}"

  common_labels = {
    application = "contentforge"
    environment = var.environment
    managed_by  = "terraform"
  }

  # APIs the agent actually calls. Enabled explicitly so a fresh project works
  # on first apply instead of failing with a confusing permission error.
  required_apis = [
    "aiplatform.googleapis.com",       # Vertex AI: Gemini + Agent Engine
    "discoveryengine.googleapis.com",  # Vertex AI Search (brand knowledge base)
    "run.googleapis.com",              # Cloud Run (agent server)
    "cloudbuild.googleapis.com",       # CI builds
    "artifactregistry.googleapis.com", # container images
    "secretmanager.googleapis.com",    # credentials
    "cloudtrace.googleapis.com",       # distributed tracing
    "logging.googleapis.com",          # structured logs
    "monitoring.googleapis.com",       # alerting
    "dlp.googleapis.com",              # PII redaction
    "sqladmin.googleapis.com",         # Cloud SQL session store
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_apis)

  project = var.project_id
  service = each.value

  # Never auto-disable on destroy: another workload in the project may depend on
  # the same API, and disabling it would break them.
  disable_on_destroy = false
}

###############################################################################
# Runtime identity
#
# A dedicated service account with only the roles the agent needs. Notably it
# does NOT get roles/editor, and it does NOT get project-wide
# secretmanager.secretAccessor - that is granted per-secret in secrets.tf.
###############################################################################

resource "google_service_account" "agent_runtime" {
  project      = var.project_id
  account_id   = "contentforge-${var.environment}-sa"
  display_name = "ContentForge agent runtime (${var.environment})"
  description  = "Runtime identity for the ContentForge Cloud Run service. Least privilege; secret access granted per-secret."
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset([
    "roles/aiplatform.user",         # invoke Gemini and Agent Engine
    "roles/discoveryengine.viewer",  # query the search datastore
    "roles/cloudtrace.agent",        # write spans
    "roles/logging.logWriter",       # write structured logs
    "roles/monitoring.metricWriter", # write metrics
    "roles/dlp.user",                # de-identify text before storage
    "roles/cloudsql.client",         # connect to the session store
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent_runtime.email}"
}

###############################################################################
# Container registry
###############################################################################

resource "google_artifact_registry_repository" "agent_images" {
  project       = var.project_id
  location      = var.region
  repository_id = "contentforge"
  format        = "DOCKER"
  description   = "ContentForge agent container images"
  labels        = local.common_labels

  # Keep the last 10 releases; expire untagged layers so storage does not grow
  # without bound.
  cleanup_policies {
    id     = "keep-recent-releases"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }

  depends_on = [google_project_service.required]
}

###############################################################################
# Cloud Run service
###############################################################################

resource "google_cloud_run_v2_service" "agent" {
  project  = var.project_id
  name     = local.service_name
  location = var.region
  labels   = local.common_labels

  deletion_protection = var.environment == "prod"

  template {
    service_account = google_service_account.agent_runtime.email

    scaling {
      # Scale to zero in non-prod to keep costs near zero between demos; keep a
      # warm instance in prod so an author never pays cold-start latency.
      min_instance_count = var.environment == "prod" ? 1 : 0
      max_instance_count = var.max_instances
    }

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
        # The agent is IO-bound on model calls; without this, CPU is throttled
        # between requests and background memory consolidation stalls.
        cpu_idle = false
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "1" # ADC via workload identity - no API key anywhere
      }
      env {
        name  = "CONTENTFORGE_ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "CONTENTFORGE_ENABLE_CLOUD_TRACE"
        value = "1"
      }
      env {
        name  = "CONTENTFORGE_ENABLE_DLP_REDACTION"
        value = var.environment == "prod" ? "1" : "0"
      }
      env {
        name  = "CONTENTFORGE_SESSION_BACKEND"
        value = "database"
      }
      # Without this the app falls back to its SQLite default, which on Cloud Run
      # means session state on an ephemeral container filesystem - silently lost
      # on every scale event. Connects over the Cloud SQL unix socket mounted by
      # the `cloudsql` volume below.
      #
      # `{db_password}` is a placeholder, not an interpolation: config.py
      # substitutes the Secret Manager value at connect time, so the credential
      # is never in the service's plaintext environment.
      env {
        name = "CONTENTFORGE_DATABASE_URL"
        value = join("", [
          "postgresql+pg8000://contentforge_agent:{db_password}@/",
          google_sql_database.sessions.name,
          "?unix_sock=/cloudsql/",
          google_sql_database_instance.sessions.connection_name,
          "/.s.PGSQL.5432",
        ])
      }
      env {
        name  = "CONTENTFORGE_REQUIRE_PUBLISH_CONFIRMATION"
        value = "1" # the human-in-the-loop gate is not configurable in deployment
      }
      env {
        name  = "CONTENTFORGE_VERTEX_SEARCH_DATASTORE"
        value = google_discovery_engine_data_store.brand_kb.name
      }

      # Secrets are injected as *references*, resolved at call time by
      # config.resolve_secret. The values never appear in the image, in
      # Terraform state, or in the service's plaintext env.
      env {
        name  = "CONTENTFORGE_CMS_API_TOKEN_SECRET"
        value = "${google_secret_manager_secret.cms_api_token.id}/versions/latest"
      }
      env {
        name  = "CONTENTFORGE_DB_PASSWORD_SECRET"
        value = "${google_secret_manager_secret.db_password.id}/versions/latest"
      }

      startup_probe {
        initial_delay_seconds = 10
        timeout_seconds       = 5
        period_seconds        = 10
        failure_threshold     = 6
        tcp_socket {
          port = 8080
        }
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.sessions.connection_name]
      }
    }

    max_instance_request_concurrency = 40
    timeout                          = "900s" # long editorial runs
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [google_project_service.required]
}

# Authenticated access only. The agent can publish to a public blog, so an
# unauthenticated endpoint would let anyone drive it.
resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = toset(var.invoker_members)

  project  = var.project_id
  location = google_cloud_run_v2_service.agent.location
  name     = google_cloud_run_v2_service.agent.name
  role     = "roles/run.invoker"
  member   = each.value
}

###############################################################################
# Alerting - surfaces the failures that matter operationally
###############################################################################

resource "google_monitoring_alert_policy" "publish_rejections" {
  count = var.enable_alerting ? 1 : 0

  project      = var.project_id
  display_name = "ContentForge (${var.environment}): human rejected a publish"
  combiner     = "OR"

  conditions {
    display_name = "publish_rejected_by_human"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_revision"
        resource.labels.service_name="${local.service_name}"
        jsonPayload.event="publish_rejected_by_human"
      EOT
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
  }

  documentation {
    content = "A reviewer rejected an agent-generated post. Repeated rejections indicate the quality gates are passing content that humans consider unpublishable - check the critic's thresholds."
  }
}

resource "google_monitoring_alert_policy" "guardrail_blocks" {
  count = var.enable_alerting ? 1 : 0

  project      = var.project_id
  display_name = "ContentForge (${var.environment}): guardrail blocked an action"
  combiner     = "OR"

  conditions {
    display_name = "guardrail_tool_blocked"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_revision"
        resource.labels.service_name="${local.service_name}"
        jsonPayload.event="guardrail.tool_blocked"
        severity>=ERROR
      EOT
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "1800s"
    }
  }

  documentation {
    content = "An agent attempted a tool call it is not authorised for. This is either a prompt-injection attempt or a genuine misconfiguration of the tool allow-list. Inspect the trace via the logged trace_id."
  }
}
