"""Observability: structured logging, distributed tracing, and PII redaction.

The three concerns are deliberately coupled through one redaction function
(:func:`content_forge.observability.redaction.redact_text`), so a field that is
unsafe to log is equally unsafe to trace or memorise, and there is exactly one
place to change when a new sensitive class is discovered.
"""

from content_forge.observability.logging_config import configure_logging, get_logger
from content_forge.observability.redaction import redact_structure, redact_text
from content_forge.observability.tracing import (
    configure_tracing,
    get_tracer,
    pipeline_span,
    set_span_attributes,
)

__all__ = [
    "configure_logging",
    "configure_tracing",
    "get_logger",
    "get_tracer",
    "pipeline_span",
    "redact_structure",
    "redact_text",
    "set_span_attributes",
]
