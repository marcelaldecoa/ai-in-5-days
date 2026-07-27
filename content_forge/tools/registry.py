"""Central tool registry.

Wrapping the raw functions in :class:`~google.adk.tools.FunctionTool` here (as
opposed to handing bare callables to each agent) buys two things:

* the human-in-the-loop requirement on publishing is declared **declaratively**
  and in one obvious place, rather than being an implementation detail buried in
  a function body; and
* the guardrail plugin can enforce a per-agent tool allow-list by name, because
  every tool name is defined exactly once, here.
"""

from __future__ import annotations

from google.adk.tools import FunctionTool

from content_forge.config import get_settings
from content_forge.tools.brand_kb import (
    retrieve_brand_style_guide,
    search_published_posts_for_overlap,
)
from content_forge.tools.memory_tools import recall_author_editorial_preferences
from content_forge.tools.publishing import (
    publish_post_to_cms,
    save_post_draft_for_human_review,
)
from content_forge.tools.research import (
    gather_supporting_evidence_for_subtopic,
    verify_claim_against_gathered_evidence,
)
from content_forge.tools.seo import score_draft_seo_readiness


def _publish_requires_confirmation(**_kwargs: object) -> bool:
    """Decide whether a publish call must be human-confirmed.

    Evaluated per call by ADK. Reading the setting at call time (rather than at
    import time) means an evaluation harness can run the mock CMS path without
    the module-level default being baked in at import.
    """
    return get_settings().require_publish_confirmation


#: Read-only research and retrieval tools. Safe for any agent to hold.
RESEARCH_TOOLS: list[FunctionTool] = [
    FunctionTool(retrieve_brand_style_guide),
    FunctionTool(search_published_posts_for_overlap),
    FunctionTool(gather_supporting_evidence_for_subtopic),
]

#: Verification tools used by the fact-checking stage.
VERIFICATION_TOOLS: list[FunctionTool] = [
    FunctionTool(verify_claim_against_gathered_evidence),
]

#: Deterministic SEO scoring.
SEO_TOOLS: list[FunctionTool] = [
    FunctionTool(score_draft_seo_readiness),
]

#: Cross-session recall.
MEMORY_TOOLS: list[FunctionTool] = [
    FunctionTool(recall_author_editorial_preferences),
]

#: Write tools. `publish_post_to_cms` is the only irreversible action in the
#: system and is gated on explicit human confirmation.
PUBLISHING_TOOLS: list[FunctionTool] = [
    FunctionTool(publish_post_to_cms, require_confirmation=_publish_requires_confirmation),
    FunctionTool(save_post_draft_for_human_review),
]

#: Names of tools that mutate anything outside the agent's own session state.
#: The guardrail plugin uses this to enforce that only the publisher agent can
#: reach them, so a prompt-injected sub-agent cannot publish.
HIGH_STAKES_TOOL_NAMES: frozenset[str] = frozenset({"publish_post_to_cms"})
