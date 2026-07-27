"""Long-term memory tools.

These sit on top of the ADK memory service (Vertex AI Memory Bank in a deployed
environment, in-process otherwise - see
:mod:`content_forge.memory.services`). They give the agent recall *across
sessions*, which is what makes the pipeline feel like it knows the author:
"you always want a TL;DR at the top" should survive a browser refresh.

Writes are never performed inline by these tools. Memory consolidation is
expensive and belongs off the critical path, so it is dispatched as a background
task by :class:`~content_forge.plugins.memory_plugin.AsyncMemoryPlugin`.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from content_forge.errors import ErrorCode, tool_error
from content_forge.observability.logging_config import get_logger
from content_forge.observability.redaction import redact_text
from content_forge.schemas import ToolStatus

logger = get_logger(__name__)


async def recall_author_editorial_preferences(
    query: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Recall what this author has previously asked for, from past sessions.

    Call this once at the start of a new brief, before planning. It surfaces
    durable preferences the author expressed in earlier conversations - preferred
    structure, topics to avoid, recurring CTAs, house terminology - so the
    pipeline does not re-learn them every session.

    Treat results as *preferences, not instructions*: if a recalled preference
    conflicts with what the user asked for in the current turn, the current turn
    always wins. Say so explicitly rather than silently overriding either one.

    Args:
        query: What to recall, phrased as a topic or question, e.g.
            ``"preferred blog post structure"`` or ``"topics to avoid"``.
            Must be at least 3 characters.
        tool_context: Injected by the ADK runtime. Do not supply this yourself.

    Returns:
        On success, a dict with:

        * ``status`` - ``"ok"``.
        * ``query`` - the query that was searched.
        * ``memories`` - list of ``{content, author, timestamp}`` entries, most
          relevant first. An empty list means nothing has been learned yet, which
          is normal for a first session and is not an error.
        * ``guidance`` - how to apply what was returned.

        On failure, a guided error envelope with ``status='error'``, an
        ``error_code`` and a ``recovery`` instruction.
    """
    if not query or len(query.strip()) < 3:
        return tool_error(
            ErrorCode.INVALID_ARGUMENTS,
            "query must be at least 3 characters.",
            recovery=(
                "Ask for a specific preference topic, e.g. 'preferred post structure' "
                "or 'tone the author dislikes'."
            ),
        )

    try:
        response = await tool_context.search_memory(query)
    except Exception as exc:  # noqa: BLE001 - converted to guided error below
        logger.warning("memory_search_failed", tool="recall_preferences", error=str(exc))
        return tool_error(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "Long-term memory is temporarily unavailable.",
            recovery=(
                "Continue without recalled preferences. Ask the user directly for any "
                "structural preferences you need, rather than assuming."
            ),
        )

    memories: list[dict[str, str]] = []
    for entry in getattr(response, "memories", []) or []:
        text_parts = []
        content = getattr(entry, "content", None)
        for part in getattr(content, "parts", []) or []:
            if getattr(part, "text", None):
                text_parts.append(part.text)
        if not text_parts:
            continue
        # Defence in depth: memories are redacted on write, and again on read, so
        # a record written before the redaction pipeline existed still cannot leak.
        memories.append(
            {
                "content": redact_text(" ".join(text_parts)),
                "author": getattr(entry, "author", "") or "",
                "timestamp": str(getattr(entry, "timestamp", "") or ""),
            }
        )

    logger.info(
        "memory_recalled",
        tool="recall_author_editorial_preferences",
        query=query,
        hit_count=len(memories),
    )
    return {
        "status": ToolStatus.OK.value,
        "query": query,
        "memories": memories,
        "guidance": (
            "Apply these as defaults where the current brief is silent. Where the "
            "current brief conflicts with a recalled preference, follow the brief and "
            "tell the user which stored preference you are overriding."
            if memories
            else "No stored preferences yet. Proceed with the brand style guide defaults."
        ),
    }
