"""Preflight diagnostics: ``make doctor``.

Answers the question you actually have before running the agent - *what is
configured, what will silently degrade, and what do I type to fix it?*

ContentForge degrades gracefully by design: no Vertex AI Search means the
bundled corpus, no Agent Engine means SQLite sessions, no DLP means regex-only
redaction. That is what makes it runnable with a bare API key. The cost of
graceful degradation is that you cannot tell from a successful startup which
mode you are actually in - so this prints it.

Exit status is 0 when the agent can run at all, 1 only when it cannot (no model
credentials). Degraded subsystems are reported, not treated as failures.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from typing import Literal

from content_forge.config import Settings, get_settings
from content_forge.models import KNOWN_MODELS, routing_table

Status = Literal["ok", "degraded", "missing"]

_MARK = {"ok": "✓", "degraded": "~", "missing": "✗"}


@dataclass
class Check:
    """One diagnostic line."""

    name: str
    status: Status
    detail: str
    fix: str = ""


def _has_module(name: str) -> bool:
    """True when an optional dependency is importable."""
    return importlib.util.find_spec(name) is not None


def _current_settings() -> Settings:
    """Read configuration afresh rather than from the cached singleton.

    `get_settings` is deliberately `lru_cache`d so hot paths do not re-parse the
    environment on every call. A diagnostic has the opposite requirement: its
    entire job is reporting what is configured *right now*, so it must see an
    edited `.env` or a just-exported variable rather than whatever was cached
    when some earlier import happened to touch it.
    """
    try:
        return Settings()
    except Exception:  # noqa: BLE001 - a malformed env must not crash the doctor
        return get_settings()


def _check_credentials() -> Check:
    """The only check that can be fatal: can we call a model at all?"""
    settings = _current_settings()
    mode = settings.credential_mode

    if mode == "api_key":
        key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
        which = "GOOGLE_API_KEY" if os.getenv("GOOGLE_API_KEY") else "GEMINI_API_KEY"
        # Never print the key. Length alone is enough to spot an empty or
        # truncated paste.
        return Check(
            "Model credentials",
            "ok",
            f"Gemini Developer API key via {which} ({len(key)} chars)",
        )

    if mode == "platform_adc":
        project = settings.project_id or "<unset>"
        if not settings.project_id:
            return Check(
                "Model credentials",
                "missing",
                "Platform mode selected but GOOGLE_CLOUD_PROJECT is unset",
                "export GOOGLE_CLOUD_PROJECT=your-project  # or use an API key instead",
            )
        return Check(
            "Model credentials",
            "ok",
            f"Gemini Enterprise Agent Platform (ADC), project {project}",
        )

    return Check(
        "Model credentials",
        "missing",
        "No API key and no platform project configured",
        "Get a key at https://aistudio.google.com/apikey then:\n"
        "        export GOOGLE_API_KEY=your-key\n"
        "        export GOOGLE_GENAI_USE_VERTEXAI=0",
    )


def _check_models() -> list[Check]:
    """Report the effective routing table and flag unrecognised model ids."""
    checks: list[Check] = []
    for entry in routing_table():
        recognised = entry["recognised"] == "true"
        checks.append(
            Check(
                f"  model.{entry['role']}",
                "ok" if recognised else "degraded",
                entry["model"] + ("" if recognised else "  (not a model id this build knows)"),
                "" if recognised else f"Valid ids: {', '.join(sorted(KNOWN_MODELS))}",
            )
        )
    return checks


def _check_sessions() -> Check:
    """Session state: durable across restarts, or lost?"""
    settings = _current_settings()
    backend = settings.session_backend

    if backend == "vertex_ai":
        if not settings.agent_engine_id:
            return Check(
                "Session state",
                "missing",
                "vertex_ai backend selected but CONTENTFORGE_AGENT_ENGINE_ID is unset",
                "Set it, or use CONTENTFORGE_SESSION_BACKEND=database",
            )
        return Check("Session state", "ok", "Agent Engine managed sessions")

    if backend == "database":
        url = settings.database_url
        if url.startswith("sqlite"):
            return Check(
                "Session state",
                "ok",
                f"SQLite at {url.split('///')[-1]} - durable across restarts",
            )
        return Check("Session state", "ok", f"{url.split(':', 1)[0]} database")

    return Check(
        "Session state",
        "degraded",
        "In-memory - a restart loses every in-progress post",
        "CONTENTFORGE_SESSION_BACKEND=database",
    )


def _check_memory() -> Check:
    """Long-term, cross-session recall."""
    settings = _current_settings()
    if settings.agent_engine_id and settings.project_id:
        return Check("Long-term memory", "ok", "Vertex AI Memory Bank")
    return Check(
        "Long-term memory",
        "degraded",
        "In-process - author preferences are not recalled in a later session",
        "Set CONTENTFORGE_AGENT_ENGINE_ID to enable Memory Bank (deployed only)",
    )


def _check_retrieval() -> Check:
    """Where research evidence and brand rules come from."""
    settings = _current_settings()
    if settings.vertex_search_datastore:
        if not _has_module("google.cloud.discoveryengine_v1"):
            return Check(
                "Retrieval",
                "degraded",
                "Datastore configured but the client library is missing - using local corpus",
                "pip install -e '.[gcp]'",
            )
        return Check("Retrieval", "ok", "Vertex AI Search datastore")
    return Check(
        "Retrieval",
        "ok",
        "Bundled corpus (8 posts, 20 sourced claims) - intended for local use",
    )


def _check_redaction() -> Check:
    """PII scrubbing before anything durable is written."""
    settings = _current_settings()
    if not settings.enable_dlp_redaction:
        return Check(
            "PII redaction",
            "ok",
            "Regex tier active (credentials, email, cards, SSN, phone, IP, IBAN)",
        )
    if not _has_module("google.cloud.dlp_v2"):
        return Check(
            "PII redaction",
            "degraded",
            "DLP requested but the client library is missing - regex tier only",
            "pip install -e '.[gcp]'",
        )
    return Check("PII redaction", "ok", "Regex tier + Cloud DLP (names, addresses)")


def _check_tracing() -> Check:
    """Where spans go."""
    settings = _current_settings()
    if settings.enable_cloud_trace and settings.project_id:
        if not _has_module("opentelemetry.exporter.cloud_trace"):
            return Check(
                "Tracing",
                "degraded",
                "Cloud Trace requested but the exporter is missing - console only",
                "pip install -e '.[gcp]'",
            )
        return Check("Tracing", "ok", "OpenTelemetry -> Cloud Trace")
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return Check("Tracing", "ok", "OpenTelemetry -> OTLP collector")
    return Check(
        "Tracing",
        "ok",
        "OpenTelemetry -> console (spans printed locally; this is expected off-cloud)",
    )


def _check_publish_gate() -> Check:
    """The one control that must never be off in a real environment."""
    settings = _current_settings()
    if settings.require_publish_confirmation:
        return Check(
            "Human publish gate",
            "ok",
            "Enabled - publishing requires explicit approval",
        )
    return Check(
        "Human publish gate",
        "degraded",
        "DISABLED - publishing will not ask for approval. Only valid for automated evaluation.",
        "CONTENTFORGE_REQUIRE_PUBLISH_CONFIRMATION=1",
    )


def _check_cms() -> Check:
    """Whether an approved publish reaches anything real."""
    settings = _current_settings()
    if settings.cms_api_token_secret:
        return Check("CMS", "ok", "Credential configured - approved posts publish for real")
    return Check(
        "CMS",
        "ok",
        "Draft-only mode - approved posts go to .contentforge/publish_outbox.jsonl "
        "and the agent says so explicitly",
    )


def run_checks() -> list[Check]:
    """Run every diagnostic, in report order."""
    checks = [_check_credentials()]
    checks.extend(_check_models())
    checks.extend(
        [
            _check_sessions(),
            _check_memory(),
            _check_retrieval(),
            _check_redaction(),
            _check_tracing(),
            _check_publish_gate(),
            _check_cms(),
        ]
    )
    return checks


def main() -> int:
    """CLI entry point. Returns 0 when the agent can run, 1 when it cannot."""
    settings = _current_settings()
    checks = run_checks()

    print("\nContentForge preflight")
    print(f"  environment: {settings.environment}")
    print(f"  credentials: {settings.credential_mode}\n")

    for check in checks:
        print(f"  {_MARK[check.status]} {check.name:<22} {check.detail}")
        if check.fix:
            # Arrow on the first line only; continuation lines align under it.
            for index, line in enumerate(check.fix.splitlines()):
                prefix = "→ " if index == 0 else "  "
                print(f"      {prefix}{line.strip()}")

    fatal = [c for c in checks if c.status == "missing"]
    degraded = [c for c in checks if c.status == "degraded"]

    print()
    if fatal:
        print(f"  {len(fatal)} blocking issue(s) - the agent cannot start.\n")
        return 1
    if degraded:
        print(
            f"  Ready, with {len(degraded)} subsystem(s) degraded. That is normal for local use.\n"
        )
    else:
        print("  Ready.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
