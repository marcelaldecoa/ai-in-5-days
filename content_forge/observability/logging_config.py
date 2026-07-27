"""Structured JSON logging.

Every log record emitted by ContentForge is a JSON object, never a formatted
string. The reason is operational: ``print(f"tool {name} failed")`` cannot be
queried, aggregated, or alerted on, whereas
``{"event": "tool_failed", "tool": "...", "trace_id": "..."}`` can be filtered in
Cloud Logging and joined against traces.

Every record carries, automatically:

* ``timestamp`` - ISO-8601 UTC.
* ``severity`` - Cloud Logging's field name, so levels render natively in the
  Logs Explorer rather than as an opaque text field.
* ``logging.googleapis.com/trace`` / ``span_id`` - injected from the active
  OpenTelemetry span, which makes every log line **click-through joinable** to
  its trace. This is what links "query" to "answer" across agents.
* ``service``, ``environment``, ``version`` - resource labels for filtering.

The last processor in the chain is :func:`_redact_processor`, so PII is scrubbed
*after* all context is attached and immediately *before* serialisation - there is
no code path that reaches the renderer un-redacted.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

import structlog

from content_forge.config import get_settings
from content_forge.observability.redaction import redact_structure

_CONFIGURED = False

#: Keys that are structural, not user data, and so bypass redaction. Redacting
#: them would corrupt trace joins (a trace id can contain a digit run that the
#: credit-card rule would otherwise eat).
_REDACTION_EXEMPT_KEYS: frozenset[str] = frozenset(
    {
        "timestamp",
        "severity",
        "level",
        "event",
        "logger",
        "logger_name",
        "service",
        "environment",
        "version",
        "trace_id",
        "span_id",
        "logging.googleapis.com/trace",
        "logging.googleapis.com/spanId",
        "invocation_id",
        "session_id",
    }
)


def _add_timestamp(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach an ISO-8601 UTC timestamp."""
    event_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
    return event_dict


def _rename_level_to_severity(
    _logger: Any, _name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Use Cloud Logging's ``severity`` field so levels are natively understood."""
    if "level" in event_dict:
        event_dict["severity"] = str(event_dict.pop("level")).upper()
    return event_dict


def _rename_logger_name(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Emit the module name as ``logger``.

    ``get_logger`` binds it as ``logger_name`` because ``logger`` is a reserved
    keyword in :func:`structlog.get_logger`; this restores the conventional field
    name on the way out.
    """
    if "logger_name" in event_dict:
        event_dict["logger"] = event_dict.pop("logger_name")
    return event_dict


def _add_service_context(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach static resource labels used for filtering in the Logs Explorer."""
    settings = get_settings()
    event_dict.setdefault("service", settings.service_name)
    event_dict.setdefault("environment", settings.environment)
    event_dict.setdefault("version", "1.0.0")
    return event_dict


def _add_trace_context(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Inject the active OpenTelemetry trace and span ids.

    This is the join key between logs and traces. With it, a single click in
    Cloud Trace surfaces every log line emitted anywhere inside that request -
    across all five agents and every tool call.
    """
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        context = span.get_span_context()
        if context.is_valid:
            trace_id = format(context.trace_id, "032x")
            span_id = format(context.span_id, "016x")
            event_dict["trace_id"] = trace_id
            event_dict["span_id"] = span_id
            project = get_settings().project_id
            if project:
                # The fully-qualified form Cloud Logging uses to render the
                # clickable trace link in the UI.
                event_dict["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace_id}"
                event_dict["logging.googleapis.com/spanId"] = span_id
    except Exception:  # noqa: BLE001 - tracing must never break logging
        pass
    return event_dict


def _redact_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Scrub PII from every field. Last processor before rendering."""
    return {
        key: value if key in _REDACTION_EXEMPT_KEYS else redact_structure(value)
        for key, value in event_dict.items()
    }


def configure_logging(force: bool = False, stream: TextIO | None = None) -> None:
    """Configure process-wide structured JSON logging. Idempotent.

    Called once from :mod:`content_forge.agent` at import time, so any entry
    point - ``adk web``, ``adk run``, the eval harness, or the Cloud Run server -
    gets identical log structure.

    Args:
        force: Re-configure even if already configured.
        stream: Where to write records. Defaults to ``sys.stderr``. Injectable so
            tests can assert on the exact rendered bytes without depending on how
            the surrounding harness has redirected the standard streams.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    sink = stream if stream is not None else sys.stderr

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            # NB: no `structlog.stdlib.add_logger_name` here - it reads
            # `logger.name`, which only exists on stdlib loggers. The module name
            # is bound explicitly in `get_logger` instead.
            _add_timestamp,
            _add_service_context,
            _add_trace_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _rename_level_to_severity,
            _rename_logger_name,
            _redact_processor,
            # JSON, always - including locally, so that what a developer debugs
            # is byte-for-byte the shape that production emits.
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sink),
        # Deliberately off. With caching enabled, a module-level logger freezes
        # its sink and processor chain on first use, so any later call to
        # `configure_logging` silently fails to reach it - the logger keeps
        # writing to the old destination. That breaks reconfiguration in tests
        # and in any host that installs its own sink after import. The saving is
        # a dictionary lookup per call, which is noise next to a model round trip.
        cache_logger_on_first_use=False,
    )

    # Route stdlib logging (ADK's own, google-genai's) through the same sink so
    # there is exactly one log format in the system.
    logging.basicConfig(format="%(message)s", stream=sink, level=level, force=True)
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger bound to ``name``.

    Args:
        name: Usually ``__name__`` of the calling module.

    Returns:
        A structlog logger that renders ``logger=<name>``. Call it with an event
        name and keyword fields:
        ``logger.info("tool_invoked", tool="score_seo", duration_ms=12)``.

    The returned object is a *lazy* proxy rather than an already-bound logger, so
    a module-level ``logger = get_logger(__name__)`` still picks up a later
    ``configure_logging`` call instead of writing to a stale sink.
    """
    configure_logging()
    # `logger` is reserved by structlog.get_logger; `_rename_logger_name`
    # restores the conventional field name during rendering.
    return structlog.get_logger(logger_name=name)
