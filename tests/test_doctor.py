"""Tests for credential-mode detection and the preflight doctor.

The property under test: someone with only a Gemini API key can run the agent,
and someone with nothing gets told exactly what to type. Both are easy to break
silently, because the failure surfaces as an opaque SDK error rather than a
clear one.
"""

from __future__ import annotations

import pytest

from content_forge.config import Settings
from content_forge.doctor import Check, run_checks


@pytest.fixture(autouse=True)
def _clear_credential_env(monkeypatch):
    """Start every test from a known-empty credential environment."""
    for var in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
    ):
        monkeypatch.delenv(var, raising=False)


def _fresh_settings(**overrides) -> Settings:
    """Settings that ignore any developer .env on the machine running the tests."""
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# Credential mode
# ---------------------------------------------------------------------------


def test_bare_api_key_selects_api_key_mode(monkeypatch):
    """The usability trap: setting only a key must not silently pick platform mode."""
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSyExampleKeyValue1234567890")
    assert _fresh_settings().credential_mode == "api_key"


def test_gemini_api_key_variable_also_works(monkeypatch):
    """The SDK accepts either name, so the doctor must too."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyExampleKeyValue1234567890")
    assert _fresh_settings().credential_mode == "api_key"


def test_explicit_platform_flag_wins_over_a_present_key(monkeypatch):
    """Every deployed path sets this to 1; it must not be overridden by a stray key."""
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSyExampleKeyValue1234567890")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "1")
    settings = _fresh_settings(project_id="p")
    assert settings.credential_mode == "platform_adc"
    assert settings.uses_vertex_ai is True


@pytest.mark.parametrize("falsy", ["0", "false", "False", "no", ""])
def test_falsy_platform_flag_selects_the_key(monkeypatch, falsy):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSyExampleKeyValue1234567890")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", falsy)
    assert _fresh_settings().credential_mode == "api_key"


def test_platform_flag_off_with_no_key_is_unconfigured(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "0")
    assert _fresh_settings().credential_mode == "unconfigured"


def test_nothing_configured_at_all_is_unconfigured():
    assert _fresh_settings(project_id="").credential_mode == "unconfigured"


def test_project_without_key_assumes_platform_credentials():
    """A project id and no key means ADC, which may well be present."""
    assert _fresh_settings(project_id="my-project").credential_mode == "platform_adc"


# ---------------------------------------------------------------------------
# Doctor output
# ---------------------------------------------------------------------------


def _by_name(checks: list[Check], name: str) -> Check:
    return next(c for c in checks if c.name.strip() == name)


def test_doctor_blocks_only_on_missing_credentials(monkeypatch):
    monkeypatch.setenv("CONTENTFORGE_PROJECT_ID", "")
    checks = run_checks()
    blocking = [c for c in checks if c.status == "missing"]
    assert all(c.name == "Model credentials" for c in blocking), (
        f"only missing credentials may block startup, got {[c.name for c in blocking]}"
    )


def test_doctor_gives_a_copy_pasteable_fix_when_unconfigured():
    credentials = _by_name(run_checks(), "Model credentials")
    assert credentials.status == "missing"
    assert "aistudio.google.com/apikey" in credentials.fix
    assert "export GOOGLE_API_KEY" in credentials.fix


def test_doctor_never_prints_the_key_itself(monkeypatch):
    secret = "AIzaSySuperSecretValueDoNotLeak12345"
    monkeypatch.setenv("GOOGLE_API_KEY", secret)
    rendered = " ".join(f"{c.name}{c.detail}{c.fix}" for c in run_checks())
    assert secret not in rendered, "the doctor leaked the API key into its output"
    assert "chars" in rendered, "length is reported instead, to spot a truncated paste"


def test_doctor_reports_every_subsystem():
    names = {c.name.strip() for c in run_checks()}
    for expected in (
        "Model credentials",
        "Session state",
        "Long-term memory",
        "Retrieval",
        "PII redaction",
        "Tracing",
        "Human publish gate",
        "CMS",
    ):
        assert expected in names, f"doctor does not report {expected!r}"


def test_doctor_reports_the_effective_model_routing():
    model_checks = [c for c in run_checks() if c.name.strip().startswith("model.")]
    assert len(model_checks) == 5, "every routed role should be reported"
    assert all("gemini-3." in c.detail for c in model_checks)


def test_doctor_flags_an_unrecognised_model_override(monkeypatch):
    monkeypatch.setenv("CONTENTFORGE_MODEL_PLANNER", "gemini-9.9-imaginary")
    planner = _by_name(run_checks(), "model.planner")
    assert planner.status == "degraded"
    assert "gemini-3.5-flash" in planner.fix, "the fix should list valid model ids"


def test_doctor_warns_when_the_publish_gate_is_disabled(monkeypatch):
    monkeypatch.setenv("CONTENTFORGE_REQUIRE_PUBLISH_CONFIRMATION", "0")
    gate = _by_name(run_checks(), "Human publish gate")
    assert gate.status == "degraded"
    assert "DISABLED" in gate.detail


def test_draft_only_mode_is_reported_as_normal_not_broken():
    """Draft-only is the correct local default, so it must not read as a failure."""
    cms = _by_name(run_checks(), "CMS")
    assert cms.status == "ok"
    assert "draft-only" in cms.detail.lower()


# ---------------------------------------------------------------------------
# .env actually reaches the environment
# ---------------------------------------------------------------------------
#
# These run in a subprocess on purpose. `.env` is loaded once, at import time,
# so a same-process test would be measuring whatever the test runner's
# environment already looked like rather than what a user actually gets.


def _run_doctor_in(cwd, extra_env: dict[str, str] | None = None):
    """Run `python -m content_forge.doctor` in `cwd` with a scrubbed environment."""
    import os
    import subprocess
    import sys

    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_CLOUD_PROJECT",
        }
    }
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "content_forge.doctor"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def test_api_key_in_dotenv_is_picked_up(tmp_path):
    """The documented quickstart: paste the key into .env, then run the agent.

    pydantic-settings reads `.env` only for its own CONTENTFORGE_ fields and does
    not export them, while the google-genai SDK reads GOOGLE_API_KEY straight
    from os.environ. Without an explicit dotenv load the quickstart silently
    fails as though no key had been supplied - which is exactly what happened
    before this was fixed.
    """
    (tmp_path / ".env").write_text(
        "GOOGLE_API_KEY=AIzaSyDotenvQuickstartKey123456\nGOOGLE_GENAI_USE_VERTEXAI=0\n",
        encoding="utf-8",
    )
    result = _run_doctor_in(tmp_path)

    assert result.returncode == 0, f"doctor reported a blocking issue:\n{result.stdout}"
    assert "credentials: api_key" in result.stdout
    assert "GOOGLE_API_KEY" in result.stdout


def test_exported_variable_beats_a_stale_dotenv(tmp_path):
    """A shell export, or a Cloud Run injection, must win over a checked-out .env."""
    (tmp_path / ".env").write_text(
        "GOOGLE_API_KEY=AIzaSyStaleValueFromDotenv123\n", encoding="utf-8"
    )
    result = _run_doctor_in(
        tmp_path,
        {"GOOGLE_API_KEY": "AIzaSyExportedValueWins456789", "GOOGLE_GENAI_USE_VERTEXAI": "0"},
    )

    assert result.returncode == 0
    # 29 chars, not the 27-char stale value - the doctor prints the length.
    assert "(29 chars)" in result.stdout, (
        f"the stale .env value appears to have won:\n{result.stdout}"
    )


def test_exported_variable_alone_works_without_any_dotenv(tmp_path):
    """The export-only path, for people who would rather not keep a .env file."""
    result = _run_doctor_in(
        tmp_path,
        {"GOOGLE_API_KEY": "AIzaSyExportOnlyNoDotenv12345", "GOOGLE_GENAI_USE_VERTEXAI": "0"},
    )
    assert result.returncode == 0
    assert "credentials: api_key" in result.stdout


def test_no_key_anywhere_still_fails_clearly(tmp_path):
    result = _run_doctor_in(tmp_path)
    assert result.returncode == 1
    assert "aistudio.google.com/apikey" in result.stdout


# ---------------------------------------------------------------------------
# Local documentation and entry points
# ---------------------------------------------------------------------------


def test_env_template_leads_with_the_api_key():
    from pathlib import Path

    template = (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "GOOGLE_API_KEY=" in template
    assert "aistudio.google.com/apikey" in template
    # The key must appear before the cloud alternative, since it is the quickstart.
    assert template.index("GOOGLE_API_KEY=") < template.index("GOOGLE_CLOUD_PROJECT")
    # And it must ship empty.
    assert "\nGOOGLE_API_KEY=\n" in template


def test_local_guide_exists_and_documents_the_quickstart():
    from pathlib import Path

    guide = (Path(__file__).parent.parent / "LOCAL.md").read_text(encoding="utf-8")
    for command in ("make env", "make doctor", "make web", "make serve"):
        assert command in guide, f"LOCAL.md does not document '{command}'"
    assert "aistudio.google.com/apikey" in guide


@pytest.mark.parametrize("target", ["doctor", "env", "serve"])
def test_local_make_targets_exist(target):
    import re
    from pathlib import Path

    makefile = (Path(__file__).parent.parent / "Makefile").read_text(encoding="utf-8")
    assert re.search(rf"^{target}:", makefile, re.MULTILINE), f"Makefile has no '{target}' target"
