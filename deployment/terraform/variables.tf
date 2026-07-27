variable "project_id" {
  description = "Google Cloud project id to deploy into."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud SQL and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment. Drives sizing, deletion protection and DLP enablement."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "container_image" {
  description = "Fully-qualified container image for the agent, produced by cloudbuild.yaml."
  type        = string
  default     = "us-central1-docker.pkg.dev/PROJECT_ID/contentforge/agent:latest"
}

variable "max_instances" {
  description = "Maximum Cloud Run instances."
  type        = number
  default     = 10
}

variable "invoker_members" {
  description = <<-EOT
    IAM members allowed to invoke the agent, e.g. ["group:editorial@example.com"].
    Deliberately not defaulted to allUsers: the agent can publish to a public
    blog, so an unauthenticated endpoint would let anyone drive it.
  EOT
  type        = list(string)
  default     = []
}

variable "vpc_network_id" {
  description = "Self-link of the VPC used for the private Cloud SQL IP. Requires private services access."
  type        = string
  default     = null
}

variable "create_gemini_api_key_secret" {
  description = "Create a Secret Manager entry for a Gemini Developer API key. Not needed when using Vertex AI with workload identity (the default)."
  type        = bool
  default     = false
}

variable "github_repository" {
  description = <<-EOT
    GitHub repository in "owner/repo" form. When set, provisions Workload
    Identity Federation so GitHub Actions can deploy without a long-lived
    service-account key. Leave empty to skip CI/CD provisioning entirely.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.github_repository == "" || can(regex("^[^/]+/[^/]+$", var.github_repository))
    error_message = "github_repository must be in 'owner/repo' form, e.g. 'octocat/hello-world'."
  }
}

variable "enable_alerting" {
  description = "Create log-based alert policies for guardrail blocks and publish rejections."
  type        = bool
  default     = true
}
