"""Strategic model routing on the Gemini Enterprise Agent Platform.

Platform note
-------------
Google renamed **Vertex AI** to the **Gemini Enterprise Agent Platform** at Cloud
Next 2026 (announced April 2026, migration completed late May 2026). The rename
is product-level only: the API endpoint, the ``google-genai`` SDK package, the
``aiplatform.v1beta1.*`` proto namespaces, and the model IDs are all unchanged,
so no code change is required to keep working.

ContentForge is unaffected by the one migration that *does* bite: the legacy
``vertexai.generative_models`` / ``vertexai.language_models`` / ``vertexai.caching``
modules stop working on 24 June 2026. This project reaches models exclusively
through ADK, which uses the current ``google-genai`` SDK, so there is nothing to
migrate.

Model lineup
------------
The 3.x line is Flash-tier only at present: **Gemini 3.5 Pro is delayed with no
announced GA date**, so there is no Pro model to route the reasoning-heavy stages
to. That is a real constraint on this routing table, not an oversight - the
strongest generally-available option is a Flash-tier model.

Generally available at time of writing:

===========================  =========================  ==========================
Model ID                     Price (in / out per 1M)    Positioning
===========================  =========================  ==========================
``gemini-3.6-flash``         $1.50 / $7.50              Newest. Tuned for complex
                                                        multi-step workflows, code
                                                        generation and multimodal
                                                        reasoning, using fewer
                                                        tokens.
``gemini-3.5-flash``         Flash tier                 Near-Pro intelligence at
                                                        Flash cost; Pro-level
                                                        coding and parallel
                                                        agentic execution.
``gemini-3.5-flash-lite``    $0.30 / $2.50              High-volume, low-reasoning
                                                        work: extraction,
                                                        classification, routing,
                                                        subagent tasks.
===========================  =========================  ==========================

Routing policy
--------------

======================  ===========================  ==============================
Role                    Model                        Why
======================  ===========================  ==============================
``planner``             ``gemini-3.5-flash``         Decomposing a brief into an
                                                     angle and outline is the
                                                     reasoning-heaviest step; a
                                                     weak plan poisons every
                                                     downstream stage.
``editorial``           ``gemini-3.5-flash``         Long-form drafting and the
                                                     critic's judgement calls need
                                                     the best available writing
                                                     and self-evaluation quality.
``research``            ``gemini-3.5-flash``         Retrieval summarisation runs
                                                     three ways in parallel; this
                                                     model is explicitly built for
                                                     parallel agentic execution.
``extraction``          ``gemini-3.5-flash-lite``    Schema-constrained field
                                                     extraction and SEO scoring
                                                     are near-mechanical. ~5x
                                                     cheaper input, ~3x cheaper
                                                     output.
``guardrail``           ``gemini-3.5-flash-lite``    Policy screening sits on the
                                                     critical path of *every*
                                                     turn, so it must add
                                                     single-digit-percent latency.
======================  ===========================  ==============================

Upgrading the reasoning tier
----------------------------
``gemini-3.6-flash`` is newer and stronger on exactly the multi-step work the
planner and critic do. Promote those two roles without touching code::

    export CONTENTFORGE_MODEL_PLANNER=gemini-3.6-flash
    export CONTENTFORGE_MODEL_EDITORIAL=gemini-3.6-flash

Every role is overridable this way, which is also how a deployment pins a model
version for reproducibility.
"""

from __future__ import annotations

import os
from typing import Final, Literal

ModelRole = Literal["planner", "editorial", "research", "extraction", "guardrail"]

#: Baseline role -> model mapping. Overridable per deployment (see module docstring).
DEFAULT_MODEL_ROUTES: Final[dict[str, str]] = {
    "planner": "gemini-3.5-flash",
    "editorial": "gemini-3.5-flash",
    "research": "gemini-3.5-flash",
    "extraction": "gemini-3.5-flash-lite",
    "guardrail": "gemini-3.5-flash-lite",
}

#: Models that exist and are GA on the platform. Used to catch a typo in a
#: `CONTENTFORGE_MODEL_*` override before it becomes a 404 mid-conversation.
KNOWN_MODELS: Final[frozenset[str]] = frozenset(
    {
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    }
)

#: Approximate USD per 1M tokens, for the cost estimate in the routing table.
#: Indicative only - authoritative pricing lives in the Google Cloud pricing page.
MODEL_PRICING_USD_PER_1M: Final[dict[str, dict[str, float]]] = {
    "gemini-3.6-flash": {"input": 1.50, "output": 7.50},
    "gemini-3.5-flash": {"input": 1.50, "output": 7.50},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
}

#: Human-readable justification, surfaced in logs and in the architecture docs so
#: the routing decision is auditable rather than folklore.
ROUTING_RATIONALE: Final[dict[str, str]] = {
    "planner": "Deep reasoning: brief decomposition and outline structure.",
    "editorial": "Highest available writing quality: drafting and self-critique.",
    "research": "Parallel agentic execution across three concurrent research angles.",
    "extraction": "Mechanical schema-constrained extraction and scoring; lowest cost tier.",
    "guardrail": "On the critical path of every turn; must stay sub-second and cheap.",
}


def resolve_model(role: ModelRole) -> str:
    """Return the model id configured for a pipeline role.

    Args:
        role: The pipeline role to route. One of ``planner``, ``editorial``,
            ``research``, ``extraction`` or ``guardrail``.

    Returns:
        A Gemini model identifier, e.g. ``"gemini-3.5-flash"``. An environment
        override (``CONTENTFORGE_MODEL_<ROLE>``) takes precedence over the
        default, and is returned even if unrecognised - pinning a brand-new model
        id must not require a code change.

    Raises:
        KeyError: If ``role`` is not a known routing role. The message lists the
            valid roles so the caller can correct the call immediately.
    """
    if role not in DEFAULT_MODEL_ROUTES:
        raise KeyError(f"Unknown model role {role!r}. Valid roles: {sorted(DEFAULT_MODEL_ROUTES)}.")
    return os.getenv(f"CONTENTFORGE_MODEL_{role.upper()}") or DEFAULT_MODEL_ROUTES[role]


def routing_table() -> list[dict[str, str]]:
    """Return the effective routing table, for logging and the ``/healthz`` payload.

    Returns:
        One dict per role with ``role``, ``model``, ``rationale`` and
        ``recognised`` keys, reflecting any environment overrides in effect.
        ``recognised`` is ``"false"`` for a model id this build does not know
        about, which is a useful warning signal in logs without being fatal.
    """
    table: list[dict[str, str]] = []
    for role in DEFAULT_MODEL_ROUTES:
        model = resolve_model(role)  # type: ignore[arg-type]
        table.append(
            {
                "role": role,
                "model": model,
                "rationale": ROUTING_RATIONALE[role],
                "recognised": "true" if model in KNOWN_MODELS else "false",
            }
        )
    return table
