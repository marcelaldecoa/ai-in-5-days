###############################################################################
# Cloud SQL - persistent session state
#
# Why a database rather than in-memory sessions: Cloud Run runs multiple
# replicas and scales to zero. An in-memory session would lose a half-written
# post whenever a container recycled or a request landed on a different replica.
# ADK's DatabaseSessionService points at this instance.
###############################################################################

resource "google_sql_database_instance" "sessions" {
  project          = var.project_id
  name             = "contentforge-${var.environment}-sessions"
  region           = var.region
  database_version = "POSTGRES_15"

  # Guard against a fat-fingered destroy of production conversation history.
  deletion_protection = var.environment == "prod"

  settings {
    tier              = var.environment == "prod" ? "db-custom-2-7680" : "db-f1-micro"
    availability_type = var.environment == "prod" ? "REGIONAL" : "ZONAL"
    disk_size         = 10
    disk_type         = "PD_SSD"
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      start_time                     = "03:00"
      point_in_time_recovery_enabled = var.environment == "prod"

      backup_retention_settings {
        retained_backups = var.environment == "prod" ? 30 : 7
      }
    }

    ip_configuration {
      # No public IP. Cloud Run reaches the instance through the Cloud SQL
      # connector, so the database is never exposed to the internet.
      ipv4_enabled = false
      # Requires a VPC with private services access; see the README's
      # "Deploying to GCP" section for the one-time network setup.
      private_network = var.vpc_network_id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    database_flags {
      name  = "log_min_duration_statement"
      value = "1000" # log queries slower than 1s
    }

    maintenance_window {
      day  = 7 # Sunday
      hour = 4
    }

    user_labels = local.common_labels
  }

  depends_on = [google_project_service.required]
}

resource "google_sql_database" "sessions" {
  project  = var.project_id
  name     = "contentforge_sessions"
  instance = google_sql_database_instance.sessions.name
}

# ---------------------------------------------------------------------------
# The database *user* is deliberately NOT managed here.
#
# Terraform cannot create a password-authenticated SQL user without the password
# passing through the configuration, and `google_sql_user.password` is persisted
# in Terraform state in plaintext. State lives in a bucket that more people can
# read than should ever see a production database credential, so managing this
# user in Terraform would undo the whole point of secrets.tf.
#
# Terraform's write-only arguments (`password_wo`) solve this, but they require
# Terraform >= 1.11 and a recent provider, which would raise the floor for
# everyone running this repo for no functional gain.
#
# So the user is created out of band by the same operator step that populates
# the Secret Manager values - see `post_apply_next_steps` in outputs.tf:
#
#   gcloud sql users create contentforge_agent \
#     --instance=<instance> --password="$DB_PASSWORD"
#
# The application never sees the literal password either: Cloud Run receives a
# connection URL containing a `{db_password}` placeholder, and
# content_forge/config.py substitutes the Secret Manager value at connect time.
# ---------------------------------------------------------------------------
