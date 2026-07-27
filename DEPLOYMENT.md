# Deploying ContentForge to Google Cloud

> **Just want to run it on your machine?** You don't need any of this — a free
> [AI Studio key](https://aistudio.google.com/apikey) is enough. See **[LOCAL.md](LOCAL.md)**.
>
> Local and deployed configuration are deliberately separate: `.env` is for your
> machine, `deployment/config.env` is for Google Cloud, and neither reads the other.

**Short version — three commands:**

```bash
make config                       # creates deployment/config.env
$EDITOR deployment/config.env     # set PROJECT_ID and INVOKER
make deploy                       # provisions everything and deploys
```

`make deploy` is idempotent. Run it again to ship an update; it provisions what's missing, updates what changed, and leaves the rest alone.

---

## What you need first

| Requirement | Check | If missing |
|---|---|---|
| Google Cloud project with **billing enabled** | `gcloud billing projects describe YOUR_PROJECT` | `gcloud projects create YOUR_PROJECT` then link a billing account |
| `gcloud` CLI, authenticated | `gcloud auth list` | [install](https://cloud.google.com/sdk/docs/install), then `gcloud auth login && gcloud auth application-default login` |
| `terraform` ≥ 1.5 | `terraform version` | [install](https://developer.hashicorp.com/terraform/install) |
| Python ≥ 3.10 and the package | `pip install -e ".[dev,gcp]"` | — |
| Project IAM role | — | **Owner**, or Editor + Project IAM Admin + Service Account Admin |

`make deploy` checks every one of these before it changes anything, and tells you exactly what to fix.

---

## Step 1 — Configure

```bash
make config
```

Creates `deployment/config.env` (git-ignored). **Two fields are required:**

```bash
PROJECT_ID="my-gcp-project"
INVOKER="user:you@example.com"     # who may call the agent
```

`INVOKER` matters. The agent can publish to a public blog, so the service is **never** deployed public — an open endpoint would let anyone drive it. Accepts `user:`, `group:`, `domain:` or `serviceAccount:`.

Everything else has a working default. The ones worth knowing:

| Field | Default | Notes |
|---|---|---|
| `DEPLOY_TARGET` | `agent-engine` | `agent-engine` = managed runtime with managed sessions + Memory Bank. `cloud-run` = our container, our Cloud SQL, our scaling policy. |
| `ENVIRONMENT` | `dev` | `prod` enables DLP redaction, regional HA, deletion protection, 30-day backups, and refuses to boot with the publish gate disabled. |
| `REGION` | `us-central1` | |
| `CMS_API_TOKEN` | empty | Empty = **draft-only mode**: approved posts go to a local outbox and the agent says plainly that nothing reached a real CMS. |
| `GITHUB_REPO` | empty | Set it to get automatic redeploys on merge to `main` (Step 4). |

---

## Step 2 — Preview (optional)

```bash
make plan
```

Runs everything up to `terraform plan` and stops. Nothing is created. Worth doing once so you can see the ~25 resources before they exist.

---

## Step 3 — Deploy

```bash
make deploy
```

Nine phases, each idempotent:

| # | Phase | What it does |
|---|---|---|
| 1 | **Preflight** | Tools, auth, project reachable, billing on, config sane |
| 2 | **APIs** | Enables the 13 Google Cloud APIs the agent uses |
| 3 | **State** | Creates the versioned Terraform state bucket |
| 4 | **Infra** | `terraform apply` — service accounts, Cloud SQL, Vertex AI Search, Secret Manager, alerting, WIF |
| 5 | **Secrets** | Moves `CMS_API_TOKEN` into Secret Manager (only if it changed) |
| 6 | **Corpus** | Uploads the brand knowledge base to the search datastore |
| 7 | **Gates** | Runs the tests and evaluation suite — **refuses to deploy a failing tree** |
| 8 | **Deploy** | `adk deploy` to Agent Engine or Cloud Run |
| 9 | **Verify + CI/CD** | Confirms it answers, prints the GitHub variables |

First run takes roughly 10–15 minutes, mostly Cloud SQL. Later runs are ~2 minutes.

### What gets created

- **2 service accounts** — a runtime identity (least privilege, no deploy rights) and, if CI/CD is enabled, a separate deployer identity. They are deliberately distinct: an agent that can redeploy itself can rewrite its own guardrails.
- **Cloud SQL Postgres**, private IP only, for durable session state
- **Vertex AI Search datastore** for the brand knowledge base
- **Secret Manager** secrets — containers only; values never pass through Terraform state
- **Artifact Registry** with a cleanup policy
- **Cloud Run service** (authenticated only) or an Agent Engine deployment
- **Log-based alerts** on guardrail blocks and human publish rejections

---

## Step 4 — Automatic redeploys

Set in `deployment/config.env`:

```bash
GITHUB_REPO="your-org/your-repo"
```

Re-run `make deploy`. It provisions Workload Identity Federation and prints the variables to set:

```
GCP_WORKLOAD_IDENTITY_PROVIDER = projects/123.../providers/github-oidc
GCP_SERVICE_ACCOUNT            = contentforge-dev-deploy@....iam.gserviceaccount.com
GCP_PROJECT_ID                 = my-gcp-project
GCP_REGION                     = us-central1
CONTENTFORGE_ENVIRONMENT       = dev
CONTENTFORGE_DEPLOY_TARGET     = agent-engine
```

Add them under **Settings → Secrets and variables → Actions → Variables**, or paste the `gh variable set` commands it prints.

From then on every merge to `main` runs `.github/workflows/deploy.yml`: tests → evaluation → deploy. Docs-only commits are skipped.

**No service-account key is ever created.** GitHub's short-lived OIDC token is exchanged for a short-lived Google token, scoped by an attribute condition to your repository only. There is no key to leak or rotate. The deployer identity can ship a revision but **cannot** read secrets or the session database.

Until those variables exist the workflow exits cleanly with a notice rather than failing, so a fork never shows a red badge.

---

## Which target should I pick?

**Agent Engine** (default) — the managed runtime on the Gemini Enterprise Agent Platform. It supplies the session service and Memory Bank that `content_forge/memory/services.py` binds to when `CONTENTFORGE_SESSION_BACKEND=vertex_ai`. Least operational work; you don't manage a container or a database.

**Cloud Run** — our multi-stage non-root container, our Cloud SQL session store, our scaling policy, and the ADK dev UI. More control, more moving parts.

Both are fully supported and neither is load-bearing for the other. Switch by changing `DEPLOY_TARGET` and re-running `make deploy`.

---

## Models

Defaults, from `content_forge/models.py`:

| Role | Model | Why |
|---|---|---|
| planner, editorial, research | `gemini-3.5-flash` | Near-Pro intelligence at Flash cost; built for parallel agentic execution |
| extraction, guardrail | `gemini-3.5-flash-lite` | Mechanical work at ~5× cheaper input, ~3× cheaper output |

**There is no Pro tier to route to** — Gemini 3.5 Pro is delayed with no announced GA date. The reasoning-heavy roles therefore sit on the strongest generally-available model, which is Flash-tier.

`gemini-3.6-flash` is newer and stronger on multi-step work. Promote the reasoning roles without touching code — uncomment in `config.env`:

```bash
MODEL_PLANNER="gemini-3.6-flash"
MODEL_EDITORIAL="gemini-3.6-flash"
```

### A note on the platform rename

Vertex AI was renamed the **Gemini Enterprise Agent Platform** at Cloud Next 2026. The rename is product-level only — the API endpoint, the `google-genai` SDK, the proto namespaces and the model IDs are unchanged, so no code change is required.

The one migration that does bite is the legacy `vertexai.generative_models` / `vertexai.language_models` / `vertexai.caching` modules, which stop working on **24 June 2026**. ContentForge reaches models exclusively through ADK on the current `google-genai` SDK and imports none of them, so there is nothing to migrate. There is a test asserting this stays true.

---

## Verifying a deployment

**Cloud Run:**
```bash
SERVICE_URL=$(gcloud run services describe contentforge-dev --region us-central1 --format='value(status.url)')
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$SERVICE_URL/list-apps"
```

**Agent Engine:**
```bash
gcloud alpha ai reasoning-engines list --region us-central1
```

**Logs and traces** (this is where the observability work pays off):
```bash
# Every tool decision, intent paired with outcome
gcloud logging read 'jsonPayload.event=~"agent.tool"' --limit 20 --format json

# Anything a guardrail blocked
gcloud logging read 'jsonPayload.event=~"guardrail"' --limit 20
```

Each log line carries a `trace_id`, so one click in Cloud Trace shows every line for that request across all nine agents.

---

## Costs

Rough monthly figures for a low-volume `dev` deployment:

| Resource | Approx. |
|---|---|
| Cloud SQL `db-f1-micro` | ~$8/mo |
| Cloud Run | ~$0 (scales to zero in non-prod) |
| Vertex AI Search | free tier for a small corpus |
| Artifact Registry | pennies |
| Gemini tokens | usage-based, ~$1.50/$7.50 per 1M in/out on Flash |

The largest fixed cost is Cloud SQL. `make destroy` removes everything when you're done.

---

## Troubleshooting

**`billing is not enabled`** — link a billing account. The platform APIs won't enable without one.

**`Permission denied` during `terraform apply`** — you need Owner, or Editor + Project IAM Admin + Service Account Admin.

**Cloud SQL fails with a private-IP error** — the private-IP path needs a one-time VPC private-services-access setup. Either complete that and pass `-var="vpc_network_id=..."`, or switch to `DEPLOY_TARGET="agent-engine"`, which doesn't use Cloud SQL.

**`adk: command not found`** — `pip install -e ".[dev,gcp]"` in the active environment.

**Deploy workflow doesn't run** — check the six repository variables from Step 4 exist. Missing ones make the workflow exit with a notice, not an error.

**Agent replies but cites nothing** — the corpus import may still be indexing. It falls back to the bundled local corpus meanwhile, which is correct behaviour, not a failure.

---

## Tearing down

```bash
make destroy
```

Asks you to type the project id first. Destroys the session database and its backups. In `prod`, deletion protection blocks this until you disable it explicitly.
