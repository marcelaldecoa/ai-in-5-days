"""Observability tests: redaction, structured logging, and intent/outcome pairing."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from content_forge.observability.redaction import redact_structure, redact_text
from content_forge.plugins.observability_plugin import IntentOutcomePlugin


class FakeTool:
    def __init__(self, name: str = "score_draft_seo_readiness") -> None:
        self.name = name


class FakeToolContext:
    def __init__(self) -> None:
        self.agent_name = "seo_reviewer_agent"
        self.invocation_id = "inv-1"
        self.function_call_id = "fc-1"
        self.state: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,leaked",
    [
        ("Reach me at jane.doe@example.com", "jane.doe@example.com"),
        ("Call +1 555-867-5309 today", "555-867-5309"),
        ("Card number 4111 1111 1111 1111", "4111 1111 1111 1111"),
        ("SSN 123-45-6789 on file", "123-45-6789"),
        ("Server at 192.168.1.100", "192.168.1.100"),
        ("token: ghp_abcdefghijklmnop123456", "ghp_abcdefghijklmnop123456"),
        ("api_key=AIzaSyD-abcdefghijklmnopqrs", "AIzaSyD-abcdefghijklmnopqrs"),
    ],
)
def test_sensitive_values_never_survive_redaction(raw, leaked):
    redacted = redact_text(raw)
    assert leaked not in redacted, f"{leaked!r} survived: {redacted!r}"
    assert "[REDACTED_" in redacted


def test_private_key_blocks_are_redacted():
    raw = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabcdef123\n-----END RSA PRIVATE KEY-----"
    redacted = redact_text(raw)
    assert "MIIEabcdef123" not in redacted
    assert "[REDACTED_CREDENTIAL]" in redacted


def test_ordinary_prose_is_untouched():
    """Over-redaction would make logs useless; the rules must be precise."""
    text = "The draft covers vector databases and scored 87.5 on the SEO check."
    assert redact_text(text) == text


def test_sensitive_keys_are_dropped_regardless_of_value():
    result = redact_structure(
        {"authorization": "Bearer anything", "api_key": "x", "topic": "vector databases"}
    )
    assert result["authorization"] == "[REDACTED_SENSITIVE_KEY]"
    assert result["api_key"] == "[REDACTED_SENSITIVE_KEY]"
    assert result["topic"] == "vector databases"


def test_redaction_recurses_into_nested_structures():
    result = redact_structure({"a": [{"b": {"c": "mail me at x@y.com"}}]})
    assert "x@y.com" not in json.dumps(result)


def test_redaction_survives_non_string_scalars():
    result = redact_structure({"count": 5, "ratio": 1.5, "flag": True, "nothing": None})
    assert result == {"count": 5, "ratio": 1.5, "flag": True, "nothing": None}


def test_redaction_guards_against_deep_nesting():
    deep: Any = "x@y.com"
    for _ in range(30):
        deep = {"n": deep}
    assert "REDACTED_DEPTH_LIMIT" in json.dumps(redact_structure(deep))


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


def _capture_logs() -> io.StringIO:
    """Point structured logging at an explicit buffer and return it.

    Asserting on an injected sink rather than on stdout/stderr keeps these tests
    independent of how pytest has redirected the standard streams - structlog
    binds its sink once at configure time, so fixture-based capture is fragile.
    """
    from content_forge.observability.logging_config import configure_logging

    buffer = io.StringIO()
    configure_logging(force=True, stream=buffer)
    return buffer


def _records(buffer: io.StringIO) -> list[dict[str, Any]]:
    """Parse the JSON records written to a capture buffer."""
    return [
        json.loads(line) for line in buffer.getvalue().strip().splitlines() if line.startswith("{")
    ]


def test_logs_are_json_with_required_fields():
    from content_forge.observability.logging_config import get_logger

    buffer = _capture_logs()
    get_logger("test.module").info("tool_invoked", tool="score_draft_seo_readiness", duration_ms=12)

    record = _records(buffer)[-1]  # must parse as JSON, not be a formatted string
    assert record["event"] == "tool_invoked"
    assert record["severity"] == "INFO"
    assert record["tool"] == "score_draft_seo_readiness"
    assert record["duration_ms"] == 12
    assert record["service"] == "contentforge"
    assert record["logger"] == "test.module"
    assert "timestamp" in record


def test_log_fields_are_redacted_before_emission():
    from content_forge.observability.logging_config import get_logger

    buffer = _capture_logs()
    get_logger("test.module").info("user_input", brief="email me at secret@corp.com")

    rendered = buffer.getvalue()
    assert "secret@corp.com" not in rendered
    assert "[REDACTED_EMAIL]" in rendered


# ---------------------------------------------------------------------------
# Intent vs outcome
# ---------------------------------------------------------------------------


async def test_intent_and_outcome_share_a_decision_id():
    buffer = _capture_logs()
    plugin = IntentOutcomePlugin()
    tool, ctx = FakeTool(), FakeToolContext()

    await plugin.before_tool_callback(
        tool=tool, tool_args={"primary_keyword": "rag"}, tool_context=ctx
    )
    await plugin.after_tool_callback(
        tool=tool,
        tool_args={"primary_keyword": "rag"},
        tool_context=ctx,
        result={"status": "ok", "score": 88.0},
    )

    records = _records(buffer)
    intent = next(r for r in records if r["event"] == "agent.tool.intent")
    outcome = next(r for r in records if r["event"] == "agent.tool.outcome")

    # The join key that makes a decision reconstructable.
    assert intent["decision_id"] == outcome["matched_intent"]
    assert intent["intended_args"] == {"primary_keyword": "rag"}
    assert outcome["status"] == "ok"
    assert outcome["intent_fulfilled"] is True
    assert outcome["duration_ms"] >= 0


async def test_failed_outcome_is_marked_unfulfilled():
    buffer = _capture_logs()
    plugin = IntentOutcomePlugin()
    tool, ctx = FakeTool(), FakeToolContext()

    await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)
    await plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=ctx, result={"status": "error"}
    )

    records = _records(buffer)
    outcome = next(r for r in records if r["event"] == "agent.tool.outcome")
    assert outcome["intent_fulfilled"] is False


async def test_tool_exception_becomes_a_guided_error():
    """An unhandled tool bug must degrade into guidance, never crash the turn."""
    plugin = IntentOutcomePlugin()
    tool, ctx = FakeTool(), FakeToolContext()

    await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)
    result = await plugin.on_tool_error_callback(
        tool=tool, tool_args={}, tool_context=ctx, error=ValueError("boom")
    )

    assert result is not None
    assert result["status"] == "error"
    assert result["error_code"] == "internal"
    assert "do not call" in result["recovery"].lower()
    assert result["correlation_id"]


async def test_long_results_are_truncated_not_dropped():
    buffer = _capture_logs()
    plugin = IntentOutcomePlugin()
    tool, ctx = FakeTool(), FakeToolContext()

    await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)
    await plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=ctx, result={"status": "ok", "draft": "x" * 5000}
    )

    records = _records(buffer)
    outcome = next(r for r in records if r["event"] == "agent.tool.outcome")
    assert "truncated" in outcome["outcome_summary"]["draft"]
    assert len(outcome["outcome_summary"]["draft"]) < 2000
