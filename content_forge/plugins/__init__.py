"""ADK plugins: cross-cutting concerns applied to every agent and tool.

Plugins rather than per-agent callbacks, because a plugin cannot be forgotten at
a call site: it applies app-wide, including to agents added later.
"""

from content_forge.plugins.guardrail_plugin import GuardrailPlugin
from content_forge.plugins.memory_plugin import AsyncMemoryPlugin
from content_forge.plugins.observability_plugin import IntentOutcomePlugin

__all__ = ["AsyncMemoryPlugin", "GuardrailPlugin", "IntentOutcomePlugin"]
