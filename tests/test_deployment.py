"""Tests over the deployment assets.

Deployment scripts are the least-exercised code in most repositories - they run
by hand, rarely, and break silently between runs. These tests assert the
properties that make the "plug and play" claim true: one config file, an
idempotent entry point, gates before shipping, and no long-lived credentials.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
DEPLOYMENT = REPO_ROOT / "deployment"
BOOTSTRAP = DEPLOYMENT / "bootstrap.sh"
CONFIG_EXAMPLE = DEPLOYMENT / "config.env.example"
TERRAFORM = DEPLOYMENT / "terraform"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"


def _terraform_source() -> str:
    """All Terraform in one string, for cross-file assertions."""
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(TERRAFORM.glob("*.tf")))


def _strip_comments(source: str, markers: tuple[str, ...] = ("#",)) -> str:
    """Drop comment lines so assertions test real directives, not prose.

    Necessary because these files *explain* the things they must not do - e.g.
    a comment stating why the service is never exposed to allUsers would
    otherwise trip a naive substring check.
    """
    kept = []
    for line in source.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(m) for m in markers):
            continue
        kept.append(line)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Single config file
# ---------------------------------------------------------------------------


def test_config_template_exists_and_is_the_only_thing_to_edit():
    assert CONFIG_EXAMPLE.exists()
    body = CONFIG_EXAMPLE.read_text(encoding="utf-8")
    assert 'PROJECT_ID=""' in body
    assert "INVOKER=" in body
    assert "DEPLOY_TARGET=" in body


def test_config_template_contains_no_real_secret():
    """The template must ship with empty credential fields."""
    body = CONFIG_EXAMPLE.read_text(encoding="utf-8")
    assert 'CMS_API_TOKEN=""' in body, "CMS_API_TOKEN must ship empty"
    leaked = re.findall(r"(sk-[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{20,})", body)
    assert not leaked, f"credential-shaped strings in the template: {leaked}"


def test_real_config_is_git_ignored():
    """config.env may hold a project id and a CMS token."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "deployment/config.env" in ignored


def test_config_env_is_not_committed():
    tracked = REPO_ROOT / "deployment" / "config.env"
    assert not tracked.exists() or "config.env" in (REPO_ROOT / ".gitignore").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Bootstrap entry point
# ---------------------------------------------------------------------------


def test_bootstrap_is_executable():
    assert BOOTSTRAP.exists(), "deployment/bootstrap.sh is missing"
    assert os.stat(BOOTSTRAP).st_mode & stat.S_IXUSR, "bootstrap.sh is not executable"


def test_bootstrap_fails_fast_on_error():
    assert "set -euo pipefail" in BOOTSTRAP.read_text(encoding="utf-8")


def test_bootstrap_runs_gates_before_deploying():
    """Shipping an untested tree is the failure this guards against."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    # Comments stripped: the header summarises the phases, so "adk deploy"
    # appears in prose long before the executable statement.
    code = _strip_comments(body)
    gates_at = min(code.index("pytest"), code.index("run_eval"))
    deploy_at = code.index("adk deploy")
    assert gates_at < deploy_at, "the test gates must run before the deploy step"
    assert "refusing to deploy" in body


def test_bootstrap_validates_prerequisites():
    body = BOOTSTRAP.read_text(encoding="utf-8")
    for check in ("billingEnabled", "gcloud auth list", "projects describe"):
        assert check in body, f"bootstrap does not check {check!r}"


def test_bootstrap_is_idempotent_about_expensive_resources():
    """Re-running must not recreate buckets or pile up secret versions."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert "buckets describe" in body, "state bucket creation is not guarded by an existence check"
    assert "versions access latest" in body, "secret writes are not guarded by a value comparison"


def test_bootstrap_supports_both_deploy_targets():
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert "adk deploy agent_engine" in body
    assert "adk deploy cloud_run" in body


def test_bootstrap_offers_a_dry_run():
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert "PLAN_ONLY" in body and "terraform plan" in body


@pytest.mark.parametrize("target", ["make config", "make deploy", "make plan", "make destroy"])
def test_documented_make_targets_exist(target):
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    name = target.split()[1]
    assert re.search(rf"^{name}:", makefile, re.MULTILINE), f"Makefile has no '{name}' target"


# ---------------------------------------------------------------------------
# CI/CD without long-lived credentials
# ---------------------------------------------------------------------------


def test_deploy_workflow_exists_and_triggers_on_main():
    assert DEPLOY_WORKFLOW.exists()
    body = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    assert "branches: [main]" in body
    assert "id-token: write" in body, "OIDC token permission is required for WIF"


def test_deploy_workflow_runs_gates_before_deploying():
    body = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    assert body.index("pytest") < body.index("adk deploy")
    assert body.index("contentforge-eval") < body.index("adk deploy")


def test_deploy_workflow_degrades_when_unconfigured():
    """A fork without GCP variables must not show a permanently red badge."""
    body = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    assert "configured" in body
    assert "::notice::" in body


def test_no_service_account_key_anywhere():
    """WIF exists precisely so no long-lived key is created or stored."""
    sources = [
        DEPLOY_WORKFLOW.read_text(encoding="utf-8"),
        BOOTSTRAP.read_text(encoding="utf-8"),
        _terraform_source(),
    ]
    for body in sources:
        assert "google_service_account_key" not in body, "a long-lived SA key is being created"
        assert "credentials_json" not in body, "a JSON key is being passed around"


def test_wif_is_scoped_to_one_repository():
    """Without the attribute condition, any GitHub repo could mint tokens."""
    source = _terraform_source()
    assert "attribute_condition" in source
    assert "assertion.repository ==" in source


def test_deployer_and_runtime_identities_are_separate():
    """An agent that can redeploy itself can rewrite its own guardrails."""
    source = _terraform_source()
    assert 'google_service_account" "deployer"' in source
    assert 'google_service_account" "agent_runtime"' in source

    # The deployer's roles must not be granted to the runtime account.
    runtime_block = source[source.index('"agent_roles"') : source.index('"agent_roles"') + 900]
    for deploy_only_role in ("roles/run.admin", "roles/artifactregistry.writer"):
        assert deploy_only_role not in runtime_block, (
            f"runtime service account must not hold {deploy_only_role}"
        )


def test_cicd_is_optional():
    """No GITHUB_REPO means no CI/CD identity is created at all."""
    source = _terraform_source()
    assert "cicd_enabled" in source
    assert 'var.github_repository != ""' in source


# ---------------------------------------------------------------------------
# Terraform shape
# ---------------------------------------------------------------------------


def test_remote_state_backend_is_configured():
    """Local state would mean no locking and no shared source of truth."""
    assert 'backend "gcs" {}' in (TERRAFORM / "main.tf").read_text(encoding="utf-8")


def test_service_is_never_deployed_publicly():
    """The agent can publish to a public blog; an open endpoint is not acceptable.

    Asserts on the actual IAM *binding* rather than the raw text: the
    ``invoker_members`` variable documents at length why it is deliberately not
    defaulted to allUsers, and that prose lives in a heredoc, not a ``#``
    comment. Matching assignments is both stricter and immune to the docs.
    """
    source = _terraform_source()
    public_binding = re.compile(
        r"members?\s*=\s*\[?\s*\"(allUsers|allAuthenticatedUsers)\"", re.MULTILINE
    )
    match = public_binding.search(source)
    assert match is None, f"a public IAM binding is declared: {match.group(0) if match else ''}"

    # And the deploy path must not add one behind Terraform's back.
    assert "--allow-unauthenticated" not in _strip_comments(BOOTSTRAP.read_text(encoding="utf-8"))
    assert "--no-allow-unauthenticated" in (DEPLOYMENT / "cloudbuild.yaml").read_text(
        encoding="utf-8"
    )


def test_terraform_declares_no_secret_values():
    """Terraform creates secret containers; values are added out of band."""
    source = _terraform_source()
    assert "google_secret_manager_secret_version" not in source, (
        "a secret value in Terraform would be persisted in state in plaintext"
    )


def test_required_apis_are_enabled_by_bootstrap():
    """A missing API surfaces as a confusing permission error at runtime."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    for api in (
        "aiplatform.googleapis.com",
        "discoveryengine.googleapis.com",
        "secretmanager.googleapis.com",
        "iamcredentials.googleapis.com",
    ):
        assert api in body, f"bootstrap does not enable {api}"


# ---------------------------------------------------------------------------
# Platform migration safety
# ---------------------------------------------------------------------------


def test_no_deprecated_vertexai_sdk_imports():
    """The legacy vertexai.* modules stop working on 24 June 2026.

    ContentForge reaches models through ADK on the current google-genai SDK.
    This test fails loudly if anyone reintroduces the deprecated path.
    """
    offenders: list[str] = []
    pattern = re.compile(r"^\s*(from|import)\s+vertexai\b", re.MULTILINE)
    for path in (REPO_ROOT / "content_forge").rglob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"deprecated vertexai.* SDK imported in: {offenders}"


def test_deployment_guide_exists_and_documents_the_three_commands():
    guide = (REPO_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    for command in ("make config", "make deploy"):
        assert command in guide, f"DEPLOYMENT.md does not document '{command}'"
    assert "Gemini Enterprise Agent Platform" in guide
