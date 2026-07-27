"""Session and memory service construction.

Two distinct kinds of state, deliberately kept separate:

**Session state (short-term, per conversation).** The event history and working
scratchpad for one brief. Backed by :class:`DatabaseSessionService` (SQLite
locally, Cloud SQL Postgres in production) or by
:class:`VertexAiSessionService` when running on Agent Engine. What matters is
that it is *not* in-process: the Cloud Run service is multi-replica and
autoscales to zero, so an in-memory session would lose a half-written post the
moment a container recycled. Durable session state is what lets an author close
their laptop mid-brief and resume on another device.

**Memory (long-term, cross-session).** Durable facts about the author and their
editorial preferences, retrieved by
:func:`~content_forge.tools.memory_tools.recall_author_editorial_preferences`.
Backed by Vertex AI Memory Bank, which performs LLM-based extraction and
consolidation of salient facts, or by an in-process service when no Agent Engine
is configured.
"""

from __future__ import annotations

from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps._configs import EventsCompactionConfig
from google.adk.memory import BaseMemoryService
from google.adk.sessions import (
    BaseSessionService,
    DatabaseSessionService,
    InMemorySessionService,
    VertexAiSessionService,
)

from content_forge.config import Settings, get_settings
from content_forge.observability.logging_config import get_logger

logger = get_logger(__name__)


def build_session_service(settings: Settings | None = None) -> BaseSessionService:
    """Construct the session service for the configured backend.

    Args:
        settings: Configuration to use. Defaults to the process settings.

    Returns:
        A :class:`~google.adk.sessions.BaseSessionService`. Falls back to
        SQLite-backed :class:`DatabaseSessionService`, and finally to
        :class:`InMemorySessionService`, if a richer backend is unavailable -
        the agent must always be able to start.
    """
    settings = settings or get_settings()

    if settings.session_backend == "vertex_ai":
        logger.info(
            "session_service", backend="vertex_ai", agent_engine_id=settings.agent_engine_id
        )
        return VertexAiSessionService(
            project=settings.project_id,
            location=settings.location,
            agent_engine_id=settings.agent_engine_id,
        )

    if settings.session_backend == "database":
        # Substitutes the Secret Manager password into the URL placeholder, so
        # the credential never sits in the process environment.
        url = settings.resolved_database_url()
        if url.startswith("sqlite"):
            # Ensure the parent directory exists before SQLAlchemy opens the file.
            settings.local_state_dir  # noqa: B018 - creates the directory
        try:
            service = DatabaseSessionService(db_url=url)
            logger.info("session_service", backend="database", dialect=url.split(":", 1)[0])
            return service
        except Exception as exc:  # noqa: BLE001 - degrade rather than fail to boot
            logger.error(
                "session_service_database_init_failed",
                error=str(exc),
                degraded_to="in_memory",
                impact="Session state will NOT survive a process restart.",
            )
            return InMemorySessionService()

    logger.warning(
        "session_service",
        backend="in_memory",
        impact="Session state will NOT survive a process restart. Use only for tests.",
    )
    return InMemorySessionService()


def build_memory_service(settings: Settings | None = None) -> BaseMemoryService:
    """Construct the long-term memory service.

    Args:
        settings: Configuration to use. Defaults to the process settings.

    Returns:
        :class:`VertexAiMemoryBankService` when an Agent Engine id is configured,
        otherwise an in-process :class:`InMemoryMemoryService`.
    """
    settings = settings or get_settings()

    if settings.agent_engine_id and settings.project_id:
        try:
            from google.adk.memory.vertex_ai_memory_bank_service import (
                VertexAiMemoryBankService,
            )

            logger.info(
                "memory_service",
                backend="vertex_ai_memory_bank",
                agent_engine_id=settings.agent_engine_id,
            )
            return VertexAiMemoryBankService(
                project=settings.project_id,
                location=settings.location,
                agent_engine_id=settings.agent_engine_id,
            )
        except Exception as exc:  # noqa: BLE001 - degrade rather than fail to boot
            logger.error("memory_service_init_failed", error=str(exc), degraded_to="in_memory")

    from google.adk.memory.in_memory_memory_service import InMemoryMemoryService

    logger.info(
        "memory_service",
        backend="in_memory",
        impact="Long-term memory will not persist across process restarts.",
    )
    return InMemoryMemoryService()


def build_compaction_config(settings: Settings | None = None) -> EventsCompactionConfig:
    """Build the history-compaction policy that bounds context growth.

    The problem this solves
    -----------------------
    An editorial session is long: a brief, a style-guide fetch, several parallel
    research calls, a draft, and then N critique/revision rounds each carrying a
    full draft. Left alone the event history grows without bound until requests
    hit the context limit, and long before that, cost and latency scale with a
    history that is mostly stale.

    ADK's compaction replaces older event spans with an LLM-generated summary.
    Two parameters govern the trade-off:

    * ``compaction_interval`` - compact every N invocations. Lower means tighter
      context but more summarisation calls.
    * ``overlap_size`` - how many events at the boundary are kept verbatim *and*
      included in the next summary. A non-zero overlap is essential: without it a
      compaction boundary can fall between a function call and its response,
      leaving the model with a dangling call it cannot interpret.

    ``token_threshold`` additionally forces compaction early when a single burst
    of activity (a long draft) blows past the budget before the interval elapses.
    ADK requires it to be paired with ``event_retention_size``, which caps how
    many recent events survive verbatim - the two together express "compact when
    the history gets expensive, but always keep the last N events intact".

    Args:
        settings: Configuration to use. Defaults to the process settings.

    Returns:
        A configured :class:`EventsCompactionConfig`.
    """
    settings = settings or get_settings()
    config = EventsCompactionConfig(
        compaction_interval=settings.compaction_interval,
        overlap_size=settings.compaction_overlap,
        token_threshold=settings.compaction_token_threshold,
        event_retention_size=settings.compaction_event_retention,
    )
    logger.info(
        "compaction_configured",
        compaction_interval=config.compaction_interval,
        overlap_size=config.overlap_size,
        token_threshold=config.token_threshold,
        event_retention_size=config.event_retention_size,
    )
    return config


def build_context_cache_config(settings: Settings | None = None) -> ContextCacheConfig:
    """Build the context-caching policy for the stable prompt prefix.

    Compaction and caching solve *different halves* of the context problem, which
    is why both are configured:

    * Compaction shrinks the part of the context that **grows** - the event
      history.
    * Caching stops paying for the part that **never changes** - the global
      constitution plus the agent's own instruction block. That prefix is
      thousands of tokens, and it is re-sent on every single model call: eight
      agents, three parallel researchers, and up to three draft/critique rounds
      per post. Provider-side caching bills it once per TTL instead of once per
      call, and cuts time-to-first-token because the prefix is not recomputed.

    Parameters:

    * ``cache_intervals`` - how many invocations a cache entry is reused before
      being refreshed.
    * ``ttl_seconds`` - lifetime of the cached prefix. Sized to comfortably span
      one editorial session so a single post never re-uploads its prefix.
    * ``min_tokens`` - floor below which caching is skipped, since caching a
      short prefix costs more in overhead than it saves.

    Args:
        settings: Configuration to use. Defaults to the process settings.

    Returns:
        A configured :class:`ContextCacheConfig`.
    """
    settings = settings or get_settings()
    config = ContextCacheConfig(
        cache_intervals=settings.cache_intervals,
        ttl_seconds=settings.cache_ttl_seconds,
        min_tokens=settings.cache_min_tokens,
    )
    logger.info(
        "context_cache_configured",
        cache_intervals=config.cache_intervals,
        ttl_seconds=config.ttl_seconds,
        min_tokens=config.min_tokens,
    )
    return config
