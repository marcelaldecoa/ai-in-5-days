"""Intent-vs-outcome instrumentation, implemented as an ADK plugin.

Why intent *and* outcome
------------------------
Logging only what happened is insufficient for debugging an agent. When a
pipeline goes wrong, the question is almost never "what did the tool return" -
it is "what did the model *think* it was doing, and how did that differ from
what actually occurred". A log that records only outcomes cannot distinguish:

* the model called the right tool with wrong arguments,
* the model called the wrong tool entirely, or
* the tool itself misbehaved.

So every tool call emits a matched pair of records sharing a ``decision_id``:

* ``agent.tool.intent`` - emitted **before execution**: the tool the model chose,
  the arguments it chose, and the agent that chose them.
* ``agent.tool.outcome`` - emitted **after execution**: status, duration, result
  summary, and ``matched_intent`` linking back.

Joining the pair on ``decision_id`` yields a complete decision record. Because
both carry the OpenTelemetry ``trace_id`` (injected by the logging processor
chain), the pair is also joinable to the span tree.

Being a *plugin* rather than per-agent callbacks matters: plugins apply to every
agent and every tool in the app, including ones added later, so instrumentation
cannot be forgotten at a call site.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins import BasePlugin
from google.adk.tools import BaseTool, ToolContext
from google.genai import types
from opentelemetry.trace import SpanKind

from content_forge.errors import ErrorCode, tool_error
from content_forge.observability.logging_config import get_logger
from content_forge.observability.redaction import redact_structure, redact_text
from content_forge.observability.tracing import get_tracer, set_span_attributes

logger = get_logger(__name__)

#: Result values longer than this are summarised rather than logged whole, so a
#: 6,000-word draft does not blow up the log volume on every revision.
_MAX_LOGGED_VALUE_CHARS = 600


def _summarise(value: Any) -> Any:
    """Redact and length-cap a value for logging."""
    redacted = redact_structure(value)
    if isinstance(redacted, str) and len(redacted) > _MAX_LOGGED_VALUE_CHARS:
        return f"{redacted[:_MAX_LOGGED_VALUE_CHARS]}... [truncated {len(redacted)} chars]"
    if isinstance(redacted, dict):
        return {
            key: (
                f"{item[:_MAX_LOGGED_VALUE_CHARS]}... [truncated {len(item)} chars]"
                if isinstance(item, str) and len(item) > _MAX_LOGGED_VALUE_CHARS
                else item
            )
            for key, item in redacted.items()
        }
    return redacted


class IntentOutcomePlugin(BasePlugin):
    """Emits paired intent/outcome telemetry for every tool and model call.

    Also acts as the last-resort error handler: any exception escaping a tool is
    converted into a guided error envelope (see :mod:`content_forge.errors`), so
    an unhandled bug degrades into actionable model guidance instead of killing
    the invocation.
    """

    def __init__(self, name: str = "intent_outcome_plugin") -> None:
        super().__init__(name=name)
        # decision_id -> (start_perf_counter, span_context_manager, span)
        self._in_flight: dict[str, tuple[float, Any, Any]] = {}

    # -- invocation lifecycle ------------------------------------------------

    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> types.Content | None:
        """Log the start of an invocation with its correlation identifiers."""
        logger.info(
            "invocation.start",
            invocation_id=invocation_context.invocation_id,
            session_id=getattr(invocation_context.session, "id", ""),
            user_id=getattr(invocation_context.session, "user_id", ""),
            root_agent=invocation_context.agent.name,
        )
        return None

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        """Log invocation completion."""
        logger.info(
            "invocation.end",
            invocation_id=invocation_context.invocation_id,
            session_id=getattr(invocation_context.session, "id", ""),
        )

    async def on_user_message_callback(
        self, *, invocation_context: InvocationContext, user_message: types.Content
    ) -> types.Content | None:
        """Record the (redacted) user intent that opened this invocation."""
        text = " ".join(part.text or "" for part in (user_message.parts or []))
        logger.info(
            "user.message",
            invocation_id=invocation_context.invocation_id,
            message_preview=_summarise(text),
            message_chars=len(text),
        )
        return None

    # -- tool calls: the intent/outcome pair ---------------------------------

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        """Record the model's *intent* before the tool runs.

        Returns None so execution proceeds normally - this callback observes, it
        does not intervene. (Policy interception lives in
        :class:`~content_forge.plugins.guardrail_plugin.GuardrailPlugin`.)
        """
        decision_id = uuid.uuid4().hex[:16]
        # Stash on the tool context so the matching outcome can find it even if
        # callbacks interleave across concurrent parallel-agent branches.
        tool_context.state[f"temp:decision_id:{tool_context.function_call_id}"] = decision_id

        span_cm = get_tracer().start_as_current_span(
            f"contentforge.tool.{tool.name}", kind=SpanKind.INTERNAL
        )
        span = span_cm.__enter__()
        set_span_attributes(
            span,
            {
                "contentforge.decision_id": decision_id,
                "contentforge.tool.name": tool.name,
                "contentforge.agent.name": tool_context.agent_name,
                "contentforge.invocation_id": tool_context.invocation_id,
                "contentforge.tool.args": _summarise(tool_args),
            },
        )
        self._in_flight[decision_id] = (time.perf_counter(), span_cm, span)

        logger.info(
            "agent.tool.intent",
            decision_id=decision_id,
            tool=tool.name,
            agent=tool_context.agent_name,
            invocation_id=tool_context.invocation_id,
            function_call_id=tool_context.function_call_id,
            intended_args=_summarise(tool_args),
            intent_description=(
                f"Agent {tool_context.agent_name!r} decided to call {tool.name!r}."
            ),
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Record the *outcome* after the tool runs, matched to its intent."""
        decision_id = self._pop_decision_id(tool_context)
        duration_ms, span = self._close_span(decision_id, result_status=_status_of(result))

        status = _status_of(result)
        logger.info(
            "agent.tool.outcome",
            decision_id=decision_id,
            matched_intent=decision_id,
            tool=tool.name,
            agent=tool_context.agent_name,
            invocation_id=tool_context.invocation_id,
            status=status,
            duration_ms=duration_ms,
            outcome_summary=_summarise(result),
            # The discrepancy signal: an intent that produced an error outcome is
            # exactly the pattern to alert on.
            intent_fulfilled=status == "ok",
        )
        if span is not None:
            set_span_attributes(span, {"contentforge.tool.status": status})
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        """Convert an unhandled tool exception into a guided error envelope.

        This is the safety net beneath the per-tool error handling: it guarantees
        that *no* tool can crash an invocation, and that the model always receives
        an actionable next step rather than a stack trace.
        """
        decision_id = self._pop_decision_id(tool_context)
        duration_ms, _ = self._close_span(decision_id, result_status="exception", error=error)

        logger.error(
            "agent.tool.outcome",
            decision_id=decision_id,
            matched_intent=decision_id,
            tool=tool.name,
            agent=tool_context.agent_name,
            invocation_id=tool_context.invocation_id,
            status="exception",
            duration_ms=duration_ms,
            error_type=type(error).__name__,
            error_message=redact_text(str(error)),
            intent_fulfilled=False,
        )
        return tool_error(
            ErrorCode.INTERNAL,
            f"The tool {tool.name!r} failed unexpectedly ({type(error).__name__}).",
            recovery=(
                f"Do not call {tool.name!r} again with identical arguments - it will "
                "fail the same way. Either continue without this tool's output and "
                "state the gap explicitly, or ask the user how to proceed. Quote "
                f"correlation id {decision_id} so an engineer can trace it."
            ),
            correlation_id=decision_id,
        )

    # -- internals -----------------------------------------------------------

    def _pop_decision_id(self, tool_context: ToolContext) -> str:
        """Retrieve the decision id recorded by ``before_tool_callback``."""
        key = f"temp:decision_id:{tool_context.function_call_id}"
        decision_id = tool_context.state.get(key)
        if isinstance(decision_id, str):
            return decision_id
        # Defensive: a tool that ran without a before-callback (rare, e.g. a
        # resumed confirmation) still gets a correlation id rather than none.
        return f"orphan-{uuid.uuid4().hex[:12]}"

    def _close_span(
        self, decision_id: str, *, result_status: str, error: Exception | None = None
    ) -> tuple[float, Any]:
        """Close the span opened for ``decision_id`` and return its duration."""
        entry = self._in_flight.pop(decision_id, None)
        if entry is None:
            return 0.0, None
        started, span_cm, span = entry
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            set_span_attributes(
                span,
                {
                    "contentforge.tool.duration_ms": duration_ms,
                    "contentforge.tool.result_status": result_status,
                },
            )
            if error is not None:
                span.record_exception(error)
            span_cm.__exit__(type(error) if error else None, error, None)
        except Exception:  # noqa: BLE001 - telemetry must never break the run
            pass
        return duration_ms, span


def _status_of(result: Any) -> str:
    """Extract the uniform ``status`` discriminator from a tool result."""
    if isinstance(result, dict):
        return str(result.get("status", "ok"))
    return "ok"
