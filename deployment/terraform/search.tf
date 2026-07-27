###############################################################################
# Vertex AI Search - the brand knowledge base
#
# Indexes the brand style guide, every published post, and the approved research
# corpus. Queried by retrieve_brand_style_guide,
# search_published_posts_for_overlap and gather_supporting_evidence_for_subtopic.
#
# When this datastore is absent the agent falls back to the bundled local corpus
# (content_forge/memory/corpus/), which is what makes the repository runnable
# without a GCP project.
###############################################################################

resource "google_discovery_engine_data_store" "brand_kb" {
  project           = var.project_id
  location          = "global"
  data_store_id     = "contentforge-${var.environment}-brand-kb"
  display_name      = "ContentForge brand knowledge base (${var.environment})"
  industry_vertical = "GENERIC"
  content_config    = "NO_CONTENT" # structured JSON records, not raw documents
  solution_types    = ["SOLUTION_TYPE_SEARCH"]

  depends_on = [google_project_service.required]
}

resource "google_discovery_engine_search_engine" "brand_kb" {
  project        = var.project_id
  engine_id      = "contentforge-${var.environment}-brand-kb-engine"
  collection_id  = "default_collection"
  location       = google_discovery_engine_data_store.brand_kb.location
  display_name   = "ContentForge brand knowledge search (${var.environment})"
  data_store_ids = [google_discovery_engine_data_store.brand_kb.data_store_id]

  search_engine_config {
    search_tier = "SEARCH_TIER_STANDARD"
  }
}

###############################################################################
# Corpus staging bucket
#
# The corpus JSON is uploaded here and imported into the datastore. Keeping the
# source of truth in Cloud Storage (and in git, under
# content_forge/memory/corpus/) means the index can always be rebuilt.
###############################################################################

resource "google_storage_bucket" "corpus" {
  project                     = var.project_id
  name                        = "${var.project_id}-contentforge-${var.environment}-corpus"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = var.environment != "prod"
  labels                      = local.common_labels

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 5
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "corpus_reader" {
  bucket = google_storage_bucket.corpus.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.agent_runtime.email}"
}
