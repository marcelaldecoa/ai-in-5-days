"""Distributed tracing with OpenTelemetry.

ADK already emits spans for agent and LLM invocations. This module does three
things on top of that:

1. **Installs a real ``TracerProvider``** with a service-identifying resource, so
   spans are attributable rather than anonymous. Exports to Cloud Trace when
   ``CONTENTFORGE_ENABLE_CLOUD_TRACE=1``, to an OTLP collector when
   ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, and to stderr otherwise - so a
   developer sees the same span tree locally that ops sees in production.
2. **Adds domain spans** for the units of work ADK does not know about
   (evidence gathering, SEO scoring, a publish approval), via
   :func:`pipeline_span`. These are the spans that make a trace *readable* as an
   editorial workflow rather than as a list of model calls.
3. **Redacts span attributes on the way in** (:func:`set_span_attributes`), for
   the same reason logs are redacted: span attributes are durably stored and
   broadly readable.

Because the tracer provider is global and the ADK spans are children of the same
context, a single trace links the user's brief through the planner, the parallel
researchers, the draft/critique loop and the publish gate down to the final URL.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from content_forge.config import get_settings
from content_forge.observability.redaction import redact_text

logger = logging.getLogger(__name__)

_TRACING_CONFIGURED = False
_TRACER_NAME = "content_forge"


def configure_tracing(force: bool = False) -> None:
    """Install the global OpenTelemetry tracer provider. Idempotent.

    Exporter selection, in priority order:

    1. ``CONTENTFORGE_ENABLE_CLOUD_TRACE=1`` -> Google Cloud Trace (batched).
    2. ``OTEL_EXPORTER_OTLP_ENDPOINT`` set -> OTLP collector (batched).
    3. Otherwise -> console exporter, so spans are visible during local
       development without any collector running.

    Args:
        force: Reinstall even if already configured. Used by tests.
    """
    global _TRACING_CONFIGURED
    if _TRACING_CONFIGURED and not force:
        return

    settings = get_settings()
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": "1.0.0",
            "deployment.environment": settings.environment,
            "cloud.provider": "gcp",
            "cloud.account.id": settings.project_id or "unset",
        }
    )
    provider = TracerProvider(resource=resource)

    exporter_installed = False
    if settings.enable_cloud_trace and settings.project_id:
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.project_id))
            )
            exporter_installed = True
            logger.info("tracing_exporter=cloud_trace project=%s", settings.project_id)
        except ImportError:
            logger.warning(
                "cloud_trace_exporter_unavailable: install with `pip install -e '.[gcp]'`"
            )

    if not exporter_installed:
        import os

        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
                exporter_installed = True
                logger.info("tracing_exporter=otlp")
            except ImportError:
                logger.warning("otlp_exporter_unavailable")

    if not exporter_installed:
        # SimpleSpanProcessor so spans flush immediately in short-lived local runs
        # instead of being lost when the process exits before a batch fires.
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _TRACING_CONFIGURED = True


def get_tracer() -> trace.Tracer:
    """Return the ContentForge tracer, configuring the provider on first use."""
    configure_tracing()
    return trace.get_tracer(_TRACER_NAME)


def set_span_attributes(span: Span, attributes: dict[str, Any]) -> None:
    """Attach redacted attributes to a span.

    All string values pass through :func:`~content_forge.observability.redaction.redact_text`
    first. Span attributes are durably stored and broadly readable, so they get
    the same treatment as logs.

    Args:
        span: The span to annotate.
        attributes: Attribute names to values. Non-scalar values are JSON-encoded
            (and redacted) rather than dropped.
    """
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, str):
            span.set_attribute(key, redact_text(value))
        elif isinstance(value, (bool, int, float)):
            span.set_attribute(key, value)
        else:
            import json

            try:
                span.set_attribute(key, redact_text(json.dumps(value, default=str)))
            except (TypeError, ValueError):
                span.set_attribute(key, redact_text(str(value)))


@contextlib.contextmanager
def pipeline_span(
    name: str, *, kind: SpanKind = SpanKind.INTERNAL, **attributes: Any
) -> Iterator[Span]:
    """Open a domain span around a unit of editorial work.

    Records exceptions and sets span status automatically, so a failure is
    visible as a red span in the trace rather than merely as a log line.

    Args:
        name: Span name, e.g. ``"contentforge.seo.score"``. Use dotted,
            low-cardinality names - never interpolate ids into the name.
        kind: OpenTelemetry span kind. Defaults to ``INTERNAL``.
        **attributes: Initial attributes, redacted via :func:`set_span_attributes`.

    Yields:
        The active :class:`~opentelemetry.trace.Span`.

    Example:
        >>> with pipeline_span("contentforge.research.gather", subtopic=topic) as span:
        ...     evidence = gather(topic)
        ...     set_span_attributes(span, {"claim_count": len(evidence)})
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name, kind=kind) as span:
        if attributes:
            set_span_attributes(span, attributes)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            raise
        else:
            span.set_status(Status(StatusCode.OK))
