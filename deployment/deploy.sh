#!/usr/bin/env bash
#
# ContentForge deployment via the ADK CLI.
#
# Two managed targets, both driven by `adk deploy` so the deployment artefact is
# built from the same agent package that `adk run` and the test suite use - there
# is no separate "deployment copy" of the agent to drift out of sync.
#
#   ./deployment/deploy.sh agent-engine     # Vertex AI Agent Engine (managed runtime)
#   ./deployment/deploy.sh cloud-run        # Cloud Run (container, full control)
#
# Terraform (deployment/terraform/) provisions the surrounding resources -
# service account, Cloud SQL, Secret Manager, Vertex AI Search. This script
# deploys the agent itself onto them. Run `terraform apply` first.
#
# Required environment:
#   GOOGLE_CLOUD_PROJECT    target project
# Optional:
#   GOOGLE_CLOUD_LOCATION   region (default us-central1)
#   CONTENTFORGE_ENVIRONMENT  dev | staging | prod (default dev)

set -euo pipefail

TARGET="${1:-}"
PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
ENVIRONMENT="${CONTENTFORGE_ENVIRONMENT:-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "error: $*" >&2; exit 1; }

[[ -n "$PROJECT" ]] || die "GOOGLE_CLOUD_PROJECT is not set."
command -v adk >/dev/null 2>&1 || die "adk not found. Run: pip install -e '.[gcp]'"

# Never deploy something the gates have not passed. This is the same suite CI
# runs, and it needs no credentials, so there is no excuse for skipping it.
echo "==> Running pre-deployment gates"
python -m pytest "${REPO_ROOT}/tests" -q
python -m content_forge.evaluation.run_eval

case "$TARGET" in
  agent-engine)
    # Agent Engine is the managed runtime: it supplies the session service and
    # the Memory Bank that content_forge.memory.services binds to when
    # CONTENTFORGE_SESSION_BACKEND=vertex_ai.
    echo "==> Deploying to Vertex AI Agent Engine (${PROJECT}/${REGION})"
    adk deploy agent_engine \
      --project "$PROJECT" \
      --region "$REGION" \
      --display_name "ContentForge (${ENVIRONMENT})" \
      --description "Multi-agent editorial pipeline: plans, researches, drafts, reviews and human-gates blog posts." \
      --trace_to_cloud \
      --otel_to_cloud \
      "${REPO_ROOT}/content_forge"

    cat <<EOF

Deployed. To bind the agent to Agent Engine managed sessions and Memory Bank,
set these on the calling service (the reasoning engine id is printed above):

  CONTENTFORGE_SESSION_BACKEND=vertex_ai
  CONTENTFORGE_AGENT_ENGINE_ID=<reasoning engine id>

EOF
    ;;

  cloud-run)
    # Cloud Run is the self-managed path: our Dockerfile, our Cloud SQL session
    # store, our scaling policy. Authenticated only - the agent can publish to a
    # public blog, so an open endpoint would let anyone drive it.
    echo "==> Deploying to Cloud Run (${PROJECT}/${REGION})"
    adk deploy cloud_run \
      --project "$PROJECT" \
      --region "$REGION" \
      --service_name "contentforge-${ENVIRONMENT}" \
      --app_name contentforge \
      --trace_to_cloud \
      --with_ui \
      "${REPO_ROOT}/content_forge"

    echo
    echo "Deployed. Grant invoke access to your editorial team:"
    echo "  gcloud run services add-iam-policy-binding contentforge-${ENVIRONMENT} \\"
    echo "    --region=${REGION} --member='group:editorial@example.com' --role='roles/run.invoker'"
    ;;

  *)
    die "usage: $0 {agent-engine|cloud-run}"
    ;;
esac
