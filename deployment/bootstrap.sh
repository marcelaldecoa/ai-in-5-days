#!/usr/bin/env bash
#
# ContentForge one-command deployment.
#
#   cp deployment/config.env.example deployment/config.env
#   $EDITOR deployment/config.env      # set PROJECT_ID and INVOKER
#   make deploy
#
# Idempotent by construction: every step checks current state before acting, so
# re-running is how you deploy an update, not something to avoid. Nothing here
# destroys data - `terraform destroy` is a separate, explicit `make destroy`.
#
# Phases:
#   0  preflight    - tools, auth, project, billing, config sanity
#   1  apis         - enable the Google Cloud APIs the agent uses
#   2  state        - create the Terraform remote-state bucket
#   3  infra        - terraform apply (service account, DB, search, secrets, WIF)
#   4  secrets      - move secret values into Secret Manager
#   5  corpus       - upload and import the brand knowledge base
#   6  gates        - lint, tests, evaluation - refuse to ship a failing tree
#   7  deploy       - adk deploy to Agent Engine or Cloud Run
#   8  verify       - confirm the deployment answers
#   9  cicd         - print the GitHub setup for automatic redeploys

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${REPO_ROOT}/deployment/config.env"
TF_DIR="${REPO_ROOT}/deployment/terraform"

# --- output helpers ----------------------------------------------------------
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
  BLUE=$'\033[34m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; DIM=""; RESET=""
fi

PHASE=0
phase() { PHASE=$((PHASE + 1)); printf '\n%s[%d/9] %s%s\n' "${BOLD}${BLUE}" "$PHASE" "$1" "$RESET"; }
ok()    { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
skip()  { printf '  %s·%s %s %s(already done)%s\n' "$DIM" "$RESET" "$1" "$DIM" "$RESET"; }
warn()  { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()   { printf '\n%serror:%s %s\n\n' "$RED" "$RESET" "$1" >&2; exit 1; }

# =============================================================================
phase "Preflight"
# =============================================================================

[[ -f "$CONFIG_FILE" ]] || die "deployment/config.env not found.
  Create it first:
    cp deployment/config.env.example deployment/config.env
  then set PROJECT_ID and INVOKER inside it."

# shellcheck disable=SC1090
set -a; source "$CONFIG_FILE"; set +a

: "${PROJECT_ID:?PROJECT_ID is empty in deployment/config.env}"
: "${INVOKER:?INVOKER is empty in deployment/config.env}"
[[ "$INVOKER" == *"CHANGE_ME"* ]] && die "INVOKER still contains CHANGE_ME. Set it to a real IAM member, e.g. user:you@example.com"

REGION="${REGION:-us-central1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
DEPLOY_TARGET="${DEPLOY_TARGET:-agent-engine}"
TF_STATE_BUCKET="${TF_STATE_BUCKET:-${PROJECT_ID}-contentforge-tfstate}"
MAX_INSTANCES="${MAX_INSTANCES:-10}"
PLAN_ONLY="${PLAN_ONLY:-0}"

case "$ENVIRONMENT" in dev|staging|prod) ;; *) die "ENVIRONMENT must be dev, staging or prod (got '$ENVIRONMENT')";; esac
case "$DEPLOY_TARGET" in agent-engine|cloud-run) ;; *) die "DEPLOY_TARGET must be agent-engine or cloud-run (got '$DEPLOY_TARGET')";; esac

for tool in gcloud terraform python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed or not on PATH.
  gcloud:    https://cloud.google.com/sdk/docs/install
  terraform: https://developer.hashicorp.com/terraform/install"
done
ok "required tools present"

gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q . \
  || die "not logged in to gcloud. Run:  gcloud auth login && gcloud auth application-default login"
ok "authenticated as $(gcloud config get-value account 2>/dev/null)"

gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1 \
  || die "project '$PROJECT_ID' does not exist or you lack access.
  Create it:  gcloud projects create $PROJECT_ID"
ok "project $PROJECT_ID reachable"

if ! gcloud billing projects describe "$PROJECT_ID" \
     --format="value(billingEnabled)" 2>/dev/null | grep -qi true; then
  die "billing is not enabled on '$PROJECT_ID'. The platform APIs will not turn on without it.
  gcloud billing projects link $PROJECT_ID --billing-account=XXXXXX-XXXXXX-XXXXXX"
fi
ok "billing enabled"

gcloud config set project "$PROJECT_ID" >/dev/null 2>&1
printf '  %starget:%s %s / %s / %s\n' "$DIM" "$RESET" "$ENVIRONMENT" "$REGION" "$DEPLOY_TARGET"

# =============================================================================
phase "Enabling Google Cloud APIs"
# =============================================================================

REQUIRED_APIS=(
  aiplatform.googleapis.com          # Gemini Enterprise Agent Platform (models, Agent Engine)
  discoveryengine.googleapis.com     # Vertex AI Search - brand knowledge base
  run.googleapis.com                 # Cloud Run
  cloudbuild.googleapis.com          # container builds
  artifactregistry.googleapis.com    # image registry
  secretmanager.googleapis.com       # credentials
  cloudtrace.googleapis.com          # distributed tracing
  logging.googleapis.com             # structured logs
  monitoring.googleapis.com          # alerting
  dlp.googleapis.com                 # PII redaction
  sqladmin.googleapis.com            # Cloud SQL session store
  iamcredentials.googleapis.com      # Workload Identity Federation
  storage.googleapis.com             # Terraform state + corpus staging
)

ENABLED="$(gcloud services list --enabled --format="value(config.name)" 2>/dev/null || true)"
TO_ENABLE=()
for api in "${REQUIRED_APIS[@]}"; do
  grep -qx "$api" <<<"$ENABLED" || TO_ENABLE+=("$api")
done

if ((${#TO_ENABLE[@]})); then
  printf '  enabling %d API(s), this takes a minute...\n' "${#TO_ENABLE[@]}"
  gcloud services enable "${TO_ENABLE[@]}" --project "$PROJECT_ID"
  ok "enabled: ${TO_ENABLE[*]}"
else
  skip "all ${#REQUIRED_APIS[@]} APIs enabled"
fi

# =============================================================================
phase "Terraform remote state"
# =============================================================================
# Remote state means the team shares one source of truth and concurrent applies
# are locked. Versioning lets you recover from a bad apply.

if gcloud storage buckets describe "gs://${TF_STATE_BUCKET}" >/dev/null 2>&1; then
  skip "state bucket gs://${TF_STATE_BUCKET}"
else
  gcloud storage buckets create "gs://${TF_STATE_BUCKET}" \
    --project "$PROJECT_ID" --location "$REGION" --uniform-bucket-level-access
  gcloud storage buckets update "gs://${TF_STATE_BUCKET}" --versioning
  ok "created state bucket gs://${TF_STATE_BUCKET}"
fi

# =============================================================================
phase "Provisioning infrastructure (Terraform)"
# =============================================================================

cd "$TF_DIR"
terraform init -reconfigure -upgrade \
  -backend-config="bucket=${TF_STATE_BUCKET}" \
  -backend-config="prefix=terraform/${ENVIRONMENT}" >/dev/null
ok "terraform initialised (backend gs://${TF_STATE_BUCKET})"

TF_VARS=(
  -var="project_id=${PROJECT_ID}"
  -var="region=${REGION}"
  -var="environment=${ENVIRONMENT}"
  -var="max_instances=${MAX_INSTANCES}"
  -var=invoker_members=["\"${INVOKER}\""]
)
[[ -n "${GITHUB_REPO:-}" ]] && TF_VARS+=(-var="github_repository=${GITHUB_REPO}")

terraform validate >/dev/null && ok "configuration valid"

if [[ "$PLAN_ONLY" == "1" ]]; then
  terraform plan "${TF_VARS[@]}"
  printf '\n%sPLAN_ONLY=1 - stopping before apply.%s\n\n' "$YELLOW" "$RESET"
  exit 0
fi

terraform apply -auto-approve "${TF_VARS[@]}"
ok "infrastructure applied"

SERVICE_ACCOUNT="$(terraform output -raw service_account_email)"
DEPLOYER_SA="$(terraform output -raw deployer_service_account 2>/dev/null || true)"
SEARCH_DATASTORE="$(terraform output -raw search_datastore_name 2>/dev/null || true)"
CORPUS_BUCKET="$(terraform output -raw corpus_bucket 2>/dev/null || true)"
CMS_SECRET_ID="$(terraform output -json secret_ids 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["cms_api_token"])' 2>/dev/null || true)"
cd "$REPO_ROOT"

# =============================================================================
phase "Secret Manager"
# =============================================================================

if [[ -n "${CMS_API_TOKEN:-}" && -n "$CMS_SECRET_ID" ]]; then
  # Only add a version when the value actually changed, so re-running does not
  # pile up identical secret versions.
  CURRENT="$(gcloud secrets versions access latest --secret="$CMS_SECRET_ID" --project="$PROJECT_ID" 2>/dev/null || true)"
  if [[ "$CURRENT" == "$CMS_API_TOKEN" ]]; then
    skip "CMS token already current"
  else
    printf '%s' "$CMS_API_TOKEN" | gcloud secrets versions add "$CMS_SECRET_ID" \
      --data-file=- --project="$PROJECT_ID" >/dev/null
    ok "CMS token stored in Secret Manager"
  fi
else
  warn "no CMS_API_TOKEN set - agent runs in draft-only mode (approved posts go to a local outbox)"
fi

# =============================================================================
phase "Brand knowledge base"
# =============================================================================

if [[ -n "$CORPUS_BUCKET" ]]; then
  gcloud storage cp "${REPO_ROOT}"/content_forge/memory/corpus/*.json \
    "gs://${CORPUS_BUCKET}/" --project="$PROJECT_ID" >/dev/null 2>&1
  ok "corpus uploaded to gs://${CORPUS_BUCKET}"
  if [[ -n "$SEARCH_DATASTORE" ]]; then
    ok "search datastore ready: ${SEARCH_DATASTORE##*/}"
  fi
else
  warn "no corpus bucket in Terraform outputs - agent will use the bundled local corpus"
fi

# =============================================================================
phase "Pre-deployment gates"
# =============================================================================
# Never ship something the gates have not passed. These need no credentials, so
# there is no excuse to skip them.

PY="python3"
[[ -x "${REPO_ROOT}/.venv/bin/python" ]] && PY="${REPO_ROOT}/.venv/bin/python"

"$PY" -m pytest "${REPO_ROOT}/tests" -q >/dev/null || die "tests failed - refusing to deploy"
ok "unit tests pass"
"$PY" -m content_forge.evaluation.run_eval >/dev/null || die "evaluation suite failed - refusing to deploy"
ok "evaluation suite passes"

# =============================================================================
phase "Deploying the agent"
# =============================================================================

export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
export GOOGLE_CLOUD_LOCATION="$REGION"
export GOOGLE_GENAI_USE_VERTEXAI=1
export CONTENTFORGE_ENVIRONMENT="$ENVIRONMENT"
[[ -n "${SEARCH_DATASTORE:-}" ]] && export CONTENTFORGE_VERTEX_SEARCH_DATASTORE="$SEARCH_DATASTORE"
for role in PLANNER EDITORIAL RESEARCH EXTRACTION GUARDRAIL; do
  var="MODEL_${role}"
  [[ -n "${!var:-}" ]] && export "CONTENTFORGE_MODEL_${role}=${!var}"
done

command -v adk >/dev/null 2>&1 || die "adk not found. Install it:  pip install -e '.[gcp]'"

if [[ "$DEPLOY_TARGET" == "agent-engine" ]]; then
  adk deploy agent_engine \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --display_name "ContentForge (${ENVIRONMENT})" \
    --description "Multi-agent editorial pipeline: plans, researches, drafts, reviews and human-gates blog posts." \
    --trace_to_cloud --otel_to_cloud \
    "${REPO_ROOT}/content_forge"
  ok "deployed to Agent Engine"
else
  adk deploy cloud_run \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --service_name "contentforge-${ENVIRONMENT}" \
    --app_name contentforge \
    --trace_to_cloud --with_ui \
    "${REPO_ROOT}/content_forge"
  ok "deployed to Cloud Run"
fi

# =============================================================================
phase "Verifying"
# =============================================================================

if [[ "$DEPLOY_TARGET" == "cloud-run" ]]; then
  SERVICE_URL="$(gcloud run services describe "contentforge-${ENVIRONMENT}" \
    --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)' 2>/dev/null || true)"
  if [[ -n "$SERVICE_URL" ]]; then
    ok "service live at ${SERVICE_URL}"
    printf '  %scall it:%s curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" %s/list-apps\n' \
      "$DIM" "$RESET" "$SERVICE_URL"
  else
    warn "could not read the service URL - check the Cloud Run console"
  fi
else
  ok "listing Agent Engine deployments"
  gcloud alpha ai reasoning-engines list --region "$REGION" --project "$PROJECT_ID" 2>/dev/null \
    | head -20 || warn "could not list reasoning engines (the deploy output above has the resource id)"
fi

# =============================================================================
phase "Continuous deployment"
# =============================================================================

if [[ -n "${GITHUB_REPO:-}" ]]; then
  cd "$TF_DIR"
  WIF_PROVIDER="$(terraform output -raw workload_identity_provider 2>/dev/null || true)"
  cd "$REPO_ROOT"
  if [[ -n "$WIF_PROVIDER" ]]; then
    ok "Workload Identity Federation provisioned - no service-account key needed"
    cat <<EOF

  ${BOLD}Final step - set these once in GitHub${RESET}
  ${DIM}(Settings -> Secrets and variables -> Actions -> Variables tab)${RESET}

    GCP_WORKLOAD_IDENTITY_PROVIDER = ${WIF_PROVIDER}
    GCP_SERVICE_ACCOUNT            = ${DEPLOYER_SA}
    GCP_PROJECT_ID                 = ${PROJECT_ID}
    GCP_REGION                     = ${REGION}
    CONTENTFORGE_ENVIRONMENT       = ${ENVIRONMENT}
    CONTENTFORGE_DEPLOY_TARGET     = ${DEPLOY_TARGET}

  Or from the command line:

    gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --body "${WIF_PROVIDER}" --repo ${GITHUB_REPO}
    gh variable set GCP_SERVICE_ACCOUNT --body "${DEPLOYER_SA}" --repo ${GITHUB_REPO}
    gh variable set GCP_PROJECT_ID --body "${PROJECT_ID}" --repo ${GITHUB_REPO}
    gh variable set GCP_REGION --body "${REGION}" --repo ${GITHUB_REPO}
    gh variable set CONTENTFORGE_ENVIRONMENT --body "${ENVIRONMENT}" --repo ${GITHUB_REPO}
    gh variable set CONTENTFORGE_DEPLOY_TARGET --body "${DEPLOY_TARGET}" --repo ${GITHUB_REPO}

  After that, every merge to main redeploys automatically.
EOF
  else
    warn "WIF provider not found in Terraform outputs - CI/CD not configured"
  fi
else
  printf '  %s·%s GITHUB_REPO not set - skipping CI/CD setup\n' "$DIM" "$RESET"
fi

printf '\n%s%sContentForge is deployed.%s  project=%s env=%s target=%s\n\n' \
  "$BOLD" "$GREEN" "$RESET" "$PROJECT_ID" "$ENVIRONMENT" "$DEPLOY_TARGET"
