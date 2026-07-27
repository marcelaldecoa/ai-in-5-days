"""Tests for the platform-level guardrails and the context-caching policy.

These cover the layers that sit *outside* our own Python controls: Vertex AI
safety filters applied at the model, and provider-side prompt caching.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from google.genai import types

from content_forge.agents.pipeline import build_root_agent
from content_forge.safety import SAFETY_SETTINGS, build_generate_content_config

REPO_ROOT = Path(__file__).parent.parent


def _all_agents(agent):
    """Yield every agent in the tree."""
    yield agent
    for sub in getattr(agent, "sub_agents", []) or []:
        yield from _all_agents(sub)


# ---------------------------------------------------------------------------
# Vertex AI safety settings
# ---------------------------------------------------------------------------


def test_all_four_harm_categories_are_covered():
    categories = {s.category for s in SAFETY_SETTINGS}
    assert categories == {
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    }


def test_no_category_is_left_unblocked():
    """BLOCK_NONE/OFF would make the setting decorative."""
    for setting in SAFETY_SETTINGS:
        assert setting.threshold not in (
            types.HarmBlockThreshold.BLOCK_NONE,
            types.HarmBlockThreshold.OFF,
        ), f"{setting.category} is effectively disabled"


def test_dangerous_content_is_deliberately_looser():
    """The blog covers security topics; a medium threshold false-positives on them."""
    by_category = {s.category: s.threshold for s in SAFETY_SETTINGS}
    assert (
        by_category[types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT]
        == types.HarmBlockThreshold.BLOCK_ONLY_HIGH
    )
    assert (
        by_category[types.HarmCategory.HARM_CATEGORY_HATE_SPEECH]
        == types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE
    )


def test_every_model_backed_agent_carries_safety_settings():
    """A single unprotected agent is a hole in the whole pipeline."""
    unprotected = []
    for agent in _all_agents(build_root_agent()):
        if not getattr(agent, "model", None):
            continue  # workflow agents make no model calls of their own
        config = getattr(agent, "generate_content_config", None)
        if not config or not config.safety_settings:
            unprotected.append(agent.name)
    assert not unprotected, f"agents without safety settings: {unprotected}"


@pytest.mark.parametrize(
    "role,expected_temperature",
    [("planner", 0.2), ("editorial", 0.7), ("research", 0.3), ("extraction", 0.0)],
)
def test_sampling_is_role_appropriate(role, expected_temperature):
    assert build_generate_content_config(role).temperature == expected_temperature


def test_deterministic_roles_are_actually_deterministic():
    """Policy screening and extraction must not vary between identical turns."""
    for role in ("extraction", "guardrail"):
        assert build_generate_content_config(role).temperature == 0.0


def test_prose_is_warmer_than_structure():
    editorial = build_generate_content_config("editorial").temperature
    planner = build_generate_content_config("planner").temperature
    assert editorial > planner


def test_unknown_role_fails_loudly():
    with pytest.raises(KeyError, match="Unknown role"):
        build_generate_content_config("nonexistent")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Context caching
# ---------------------------------------------------------------------------


def test_context_cache_is_configured_on_the_app():
    from content_forge.agent import build_app

    config = build_app().context_cache_config
    assert config is not None, "context caching is not wired into the App"
    assert config.ttl_seconds > 0
    assert config.cache_intervals > 0


def test_cache_min_tokens_avoids_caching_trivial_prefixes():
    """Caching a short prefix costs more overhead than it saves."""
    from content_forge.memory.services import build_context_cache_config

    assert build_context_cache_config().min_tokens > 0


def test_cache_ttl_spans_an_editorial_session():
    """A TTL shorter than a session would re-upload the prefix mid-post."""
    from content_forge.memory.services import build_context_cache_config

    assert build_context_cache_config().ttl_seconds >= 600


def test_compaction_and_caching_are_both_present():
    """They solve different halves of the context problem; neither replaces the other."""
    from content_forge.agent import build_app

    app = build_app()
    assert app.events_compaction_config is not None, "history growth is unmanaged"
    assert app.context_cache_config is not None, "the stable prefix is re-billed every call"


# ---------------------------------------------------------------------------
# Deployment entry point
# ---------------------------------------------------------------------------


def test_deploy_script_is_executable_and_valid():
    script = REPO_ROOT / "deployment" / "deploy.sh"
    assert script.exists(), "deployment/deploy.sh is missing"
    assert os.stat(script).st_mode & stat.S_IXUSR, "deploy.sh is not executable"

    body = script.read_text(encoding="utf-8")
    # Both managed targets must be reachable through the ADK CLI.
    assert "adk deploy agent_engine" in body
    assert "adk deploy cloud_run" in body
    # Deploying something the gates have not passed is the failure mode this guards.
    assert "pytest" in body and "run_eval" in body
    assert "set -euo pipefail" in body
