"""ContentForge application entry point.

This module is what ``adk run content_forge``, ``adk web`` and
``adk deploy`` load. It assembles the pieces built elsewhere in the package:

* the agent tree                  -> :mod:`content_forge.agents.pipeline`
* the plugins                     -> :mod:`content_forge.plugins`
* history compaction              -> :func:`content_forge.memory.services.build_compaction_config`
* persistent sessions and memory  -> :mod:`content_forge.memory.services`
* logging and tracing             -> :mod:`content_forge.observability`

Two objects are exported:

``root_agent``
    The bare agent tree, which is what the ADK CLI discovers by convention.

``app``
    The full :class:`~google.adk.apps.app.App` - agent tree *plus* plugins,
    compaction and resumability. Prefer this: constructing a
    :class:`~google.adk.runners.Runner` from ``root_agent`` alone silently drops
    the guardrails and the telemetry.
"""

from __future__ import annotations

from google.adk.apps._configs import ResumabilityConfig
from google.adk.apps.app import App
from google.adk.runners import Runner

from content_forge.agents.pipeline import build_root_agent
from content_forge.config import get_settings
from content_forge.memory.services import (
    build_compaction_config,
    build_context_cache_config,
    build_memory_service,
    build_session_service,
)
from content_forge.memory.vector_store import get_knowledge_base
from content_forge.models import routing_table
from content_forge.observability.logging_config import configure_logging, get_logger
from content_forge.observability.tracing import configure_tracing
from content_forge.plugins import AsyncMemoryPlugin, GuardrailPlugin, IntentOutcomePlugin

# Observability is configured at import time so that *every* entry point - the
# ADK CLI, the web server, the eval harness - emits identically structured logs
# and traces. There is no code path that logs unstructured output.
configure_logging()
configure_tracing()

logger = get_logger(__name__)

APP_NAME = "contentforge"


def build_plugins() -> list:
    """Construct the app-wide plugin chain, in execution order.

    Order matters. The guardrail plugin is registered first so its
    ``before_tool_callback`` can veto a call *before* the observability plugin
    records an intent for work that will never happen.

    Returns:
        The plugin instances, with brand banned-phrases preloaded into the
        guardrail from the style guide.
    """
    guardrail = GuardrailPlugin()
    try:
        # Preload the brand's banned phrases so output screening works from the
        # first turn, rather than only after a style-guide tool call.
        style_guide = get_knowledge_base().fetch_style_guide(
            topic="general", content_type="blog_post"
        )
        guardrail.set_banned_phrases(style_guide.banned_phrases)
    except Exception as exc:  # noqa: BLE001 - guardrail still works without the list
        logger.warning("banned_phrase_preload_failed", error=str(exc))

    return [guardrail, IntentOutcomePlugin(), AsyncMemoryPlugin()]


def build_app() -> App:
    """Assemble the complete ContentForge application.

    Returns:
        An :class:`~google.adk.apps.app.App` with the agent tree, the plugin
        chain, history compaction and resumability configured.
    """
    settings = get_settings()
    app = App(
        name=APP_NAME,
        root_agent=build_root_agent(),
        plugins=build_plugins(),
        # Bounds context growth over a long editorial session. See
        # build_compaction_config for the parameter rationale.
        events_compaction_config=build_compaction_config(settings),
        # The other half of context management: compaction shrinks what grows
        # (event history), caching stops paying repeatedly for what never
        # changes (the constitution + instruction prefix, re-sent on every call
        # across eight agents and up to three revision rounds).
        context_cache_config=build_context_cache_config(settings),
        # Required for the human-in-the-loop publish gate: the invocation is
        # suspended awaiting approval and resumed later, potentially in a
        # different process after an autoscale event.
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    logger.info(
        "app_initialised",
        app_name=APP_NAME,
        environment=settings.environment,
        session_backend=settings.session_backend,
        model_routes=routing_table(),
        plugins=[plugin.name for plugin in app.plugins],
        publish_confirmation_required=settings.require_publish_confirmation,
    )
    return app


def build_runner() -> Runner:
    """Build a :class:`~google.adk.runners.Runner` wired to persistent services.

    Used by the evaluation harness and by any custom server entry point. The ADK
    CLI builds its own runner from ``app``.

    Returns:
        A runner with persistent session and long-term memory services attached.
    """
    settings = get_settings()
    return Runner(
        app=build_app(),
        session_service=build_session_service(settings),
        memory_service=build_memory_service(settings),
    )


#: The full application. Prefer this over ``root_agent``.
app = build_app()

#: The bare agent tree, discovered by the ADK CLI by convention.
root_agent = app.root_agent
