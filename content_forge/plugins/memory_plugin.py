"""Asynchronous memory consolidation.

The problem
-----------
Writing a session into long-term memory is expensive: Vertex AI Memory Bank runs
LLM-based extraction and consolidation over the transcript, which takes seconds.
Doing that inline at the end of a turn means the user watches a spinner *after*
their post is already finished - pure added latency for work whose result they
will not see until some future session.

The fix
-------
Dispatch consolidation as a background :class:`asyncio.Task` and return
immediately. The user's turn completes at the speed of the work they asked for;
memory catches up behind them.

Doing this safely takes more than a bare ``create_task``:

* **Strong references are retained** in ``_pending``. An un-referenced task can
  be garbage-collected mid-flight, which silently drops the write.
* **A bounded semaphore** caps concurrent consolidations, so a burst of finishing
  sessions cannot exhaust the memory service quota.
* **Exceptions are captured and logged**, not swallowed - a background task that
  raises into the void is invisible in production.
* **Content is redacted before it is written**, because memory is re-injected
  into future prompts: an un-redacted write is a permanent leak, not a transient
  one.
* **:meth:`close` drains in-flight tasks** on shutdown so a Cloud Run SIGTERM
  during scale-down does not lose the last few writes.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins import BasePlugin

from content_forge.observability.logging_config import get_logger
from content_forge.observability.redaction import redact_text
from content_forge.observability.tracing import pipeline_span

logger = get_logger(__name__)

#: Maximum concurrent background consolidations per process.
_MAX_CONCURRENT_CONSOLIDATIONS = 4
#: Seconds to wait for in-flight consolidations during shutdown.
_SHUTDOWN_DRAIN_TIMEOUT = 10.0


class AsyncMemoryPlugin(BasePlugin):
    """Consolidates finished sessions into long-term memory, off the critical path."""

    def __init__(self, name: str = "async_memory_plugin") -> None:
        super().__init__(name=name)
        self._pending: set[asyncio.Task[Any]] = set()
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CONSOLIDATIONS)

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        """Schedule memory consolidation without blocking the response.

        Returns as soon as the task is *scheduled*, not when it completes, so the
        user's turn is never delayed by memory work.
        """
        memory_service = getattr(invocation_context, "memory_service", None)
        session = getattr(invocation_context, "session", None)
        if memory_service is None or session is None:
            return

        task = asyncio.create_task(
            self._consolidate(memory_service, session, invocation_context.invocation_id),
            name=f"memory-consolidation-{invocation_context.invocation_id}",
        )
        # Retain a strong reference; without it the event loop may collect the
        # task before it runs and the write is silently lost.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

        logger.info(
            "memory.consolidation.scheduled",
            invocation_id=invocation_context.invocation_id,
            session_id=getattr(session, "id", ""),
            pending_tasks=len(self._pending),
            blocking=False,
        )

    async def _consolidate(self, memory_service: Any, session: Any, invocation_id: str) -> None:
        """Redact and write a finished session into long-term memory."""
        async with self._semaphore:
            with pipeline_span(
                "contentforge.memory.consolidate",
                invocation_id=invocation_id,
                session_id=getattr(session, "id", ""),
            ):
                try:
                    self._redact_session_in_place(session)
                    await memory_service.add_session_to_memory(session)
                    logger.info(
                        "memory.consolidation.completed",
                        invocation_id=invocation_id,
                        session_id=getattr(session, "id", ""),
                        event_count=len(getattr(session, "events", []) or []),
                    )
                except asyncio.CancelledError:
                    logger.warning("memory.consolidation.cancelled", invocation_id=invocation_id)
                    raise
                except Exception as exc:  # noqa: BLE001 - background task must not die silently
                    logger.error(
                        "memory.consolidation.failed",
                        invocation_id=invocation_id,
                        error_type=type(exc).__name__,
                        error_message=redact_text(str(exc)),
                        impact=(
                            "Preferences from this session will not be recalled later. "
                            "The user-visible turn was unaffected."
                        ),
                    )

    @staticmethod
    def _redact_session_in_place(session: Any) -> None:
        """Scrub PII from every text part before the session is persisted.

        Memory is re-injected into future prompts, so anything stored here is
        effectively permanent. This is the last redaction checkpoint before that
        happens.
        """
        for event in getattr(session, "events", []) or []:
            content = getattr(event, "content", None)
            for part in getattr(content, "parts", []) or []:
                text = getattr(part, "text", None)
                if text:
                    with contextlib.suppress(AttributeError, ValueError):
                        part.text = redact_text(text)

    async def close(self) -> None:
        """Drain in-flight consolidations on shutdown.

        Called by the ADK runner during teardown. Without this, a Cloud Run
        SIGTERM during scale-down would abandon the last few memory writes.
        """
        if not self._pending:
            return
        logger.info("memory.consolidation.draining", pending_tasks=len(self._pending))
        done, still_pending = await asyncio.wait(self._pending, timeout=_SHUTDOWN_DRAIN_TIMEOUT)
        if still_pending:
            logger.warning(
                "memory.consolidation.drain_timeout",
                abandoned_tasks=len(still_pending),
                completed_tasks=len(done),
            )
            for task in still_pending:
                task.cancel()
