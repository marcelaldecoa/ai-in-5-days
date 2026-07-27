"""Multi-agent composition.

Topology
--------

::

    editorial_coordinator                      (LlmAgent - Coordinator/Dispatcher)
    |
    +-- content_planning_pipeline              (SequentialAgent)
    |   |
    |   +-- planner_agent                      (LlmAgent, Pro, output_schema=ContentPlan)
    |   |
    |   +-- parallel_research_team             (ParallelAgent - fan-out)
    |   |   +-- research_agent_foundations     (LlmAgent, Flash)
    |   |   +-- research_agent_evidence        (LlmAgent, Flash)
    |   |   +-- research_agent_counterpoints   (LlmAgent, Flash)
    |   |
    |   +-- draft_revision_loop                (LoopAgent - Generator/Critic)
    |   |   +-- drafter_agent                  (LlmAgent, Pro)
    |   |   +-- critic_agent                   (LlmAgent, Pro, output_schema=DraftCritique)
    |   |
    |   +-- seo_reviewer_agent                 (LlmAgent, Flash-Lite)
    |
    +-- publisher_agent                        (LlmAgent, Flash - HITL-gated tools)

Why these patterns
------------------
* **Coordinator/Dispatcher** at the root. A monolithic agent holding all eleven
  tools picks the wrong one under load and carries every specialist's
  instructions in every prompt. Delegation keeps each context small and each
  instruction set focused.
* **Sequential** for the pipeline. Planning, research, drafting and review have a
  genuine data dependency - you cannot draft what you have not researched - so
  the ordering is expressed structurally rather than hoped for in a prompt.
* **Parallel** for research. The three research angles are independent, so
  running them concurrently cuts the slowest stage's wall-clock to roughly a
  third. This is also why research is routed to Flash: three concurrent Pro calls
  would cost more than the quality gain justifies.
* **Loop** for draft/critique. Quality converges through iteration, not in one
  shot. The loop runs at most ``MAX_REVISION_ROUNDS`` and exits early via
  :func:`exit_loop` the moment the critic reports ``passes_quality_bar``.

State flows between stages through ``output_key``, so each agent reads its
predecessor's structured output from session state rather than from a re-parsed
conversation transcript.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.tools import exit_loop

from content_forge.models import resolve_model
from content_forge.prompts import (
    COORDINATOR_INSTRUCTION,
    CRITIC_INSTRUCTION,
    DRAFTER_INSTRUCTION,
    GLOBAL_CONSTITUTION,
    PLANNER_INSTRUCTION,
    PUBLISHER_INSTRUCTION,
    RESEARCHER_INSTRUCTION,
    SEO_REVIEWER_INSTRUCTION,
)
from content_forge.safety import build_generate_content_config
from content_forge.schemas import ContentPlan, DraftCritique
from content_forge.tools.registry import (
    MEMORY_TOOLS,
    PUBLISHING_TOOLS,
    RESEARCH_TOOLS,
    SEO_TOOLS,
    VERIFICATION_TOOLS,
)

#: Upper bound on draft/critique iterations. Three rounds captures nearly all
#: achievable quality gain; beyond that the critic tends to trade one structural
#: nit for another without improving the post.
MAX_REVISION_ROUNDS = 3


def build_planner_agent() -> LlmAgent:
    """Build the planning agent.

    Routed to the ``planner`` model (Pro): the plan determines the quality
    ceiling for every downstream stage, so this is where reasoning capacity pays
    for itself. ``output_schema=ContentPlan`` constrains decoding so the sequence
    cannot proceed on a malformed plan.
    """
    return LlmAgent(
        name="planner_agent",
        model=resolve_model("planner"),
        generate_content_config=build_generate_content_config("planner"),
        description=(
            "Turns a topic brief into a structured content plan: angle, audience, "
            "keywords and section outline, checked against prior posts for "
            "keyword cannibalisation."
        ),
        instruction=PLANNER_INSTRUCTION,
        tools=list(RESEARCH_TOOLS),
        output_schema=ContentPlan,
        output_key="content_plan",
        # The plan is derived from the brief, not from chat history; excluding
        # history keeps the planning prompt small and reproducible.
        disallow_transfer_to_peers=True,
    )


def build_research_team() -> ParallelAgent:
    """Build the fan-out research stage.

    Three researchers work concurrently on complementary angles. Splitting by
    *angle* rather than by *section* is deliberate: it makes the three contexts
    genuinely disjoint, so they do not duplicate retrieval effort.
    """
    angles = [
        (
            "research_agent_foundations",
            "Establishes definitions, mechanisms and background for the topic.",
            "Focus on foundational explanation: what the thing is, how it works, "
            "and the accepted definitions. Research the plan's early sections.",
        ),
        (
            "research_agent_evidence",
            "Gathers quantitative evidence, benchmarks and measured results.",
            "Focus on numbers: benchmarks, measured outcomes, published results. "
            "Every figure must carry the methodology and source that produced it.",
        ),
        (
            "research_agent_counterpoints",
            "Finds limitations, failure modes, and credible dissenting views.",
            "Focus on the other side: known limitations, failure modes, criticisms "
            "and trade-offs. A post that only presents upside is not credible.",
        ),
    ]
    return ParallelAgent(
        name="parallel_research_team",
        description=(
            "Runs three complementary research angles concurrently and merges their "
            "evidence for the drafter."
        ),
        sub_agents=[
            LlmAgent(
                name=name,
                model=resolve_model("research"),
                generate_content_config=build_generate_content_config("research"),
                description=description,
                instruction=f"{RESEARCHER_INSTRUCTION}\n\n## Your assigned angle\n\n{focus}",
                tools=list(RESEARCH_TOOLS),
                output_key=f"evidence_{name.rsplit('_', 1)[-1]}",
            )
            for name, description, focus in angles
        ],
    )


def build_draft_revision_loop() -> LoopAgent:
    """Build the generator/critic refinement loop.

    The critic holds :func:`exit_loop`, so the loop terminates on *quality*
    rather than on a fixed iteration count - and ``max_iterations`` caps the cost
    if quality never converges.
    """
    drafter = LlmAgent(
        name="drafter_agent",
        model=resolve_model("editorial"),
        generate_content_config=build_generate_content_config("editorial"),
        description="Writes and revises the post from the plan and gathered evidence.",
        instruction=DRAFTER_INSTRUCTION,
        tools=list(VERIFICATION_TOOLS),
        output_key="current_draft",
    )
    critic = LlmAgent(
        name="critic_agent",
        model=resolve_model("editorial"),
        generate_content_config=build_generate_content_config("editorial"),
        description=(
            "Adversarially reviews the draft for factual, brand and structural "
            "defects, and ends the revision loop once it passes."
        ),
        instruction=CRITIC_INSTRUCTION,
        tools=[exit_loop, *VERIFICATION_TOOLS],
        output_schema=DraftCritique,
        output_key="critique",
    )
    return LoopAgent(
        name="draft_revision_loop",
        description=(
            "Iteratively drafts and critiques until the post clears the quality bar "
            "or the revision budget is exhausted."
        ),
        sub_agents=[drafter, critic],
        max_iterations=MAX_REVISION_ROUNDS,
    )


def build_seo_reviewer_agent() -> LlmAgent:
    """Build the SEO review stage.

    Routed to ``extraction`` (Flash-Lite): the scoring itself is deterministic
    Python, so the model only formats and explains findings.
    """
    return LlmAgent(
        name="seo_reviewer_agent",
        model=resolve_model("extraction"),
        generate_content_config=build_generate_content_config("extraction"),
        description="Scores the finished draft for search readiness and reports blockers.",
        instruction=SEO_REVIEWER_INSTRUCTION,
        tools=list(SEO_TOOLS),
        output_key="seo_report",
    )


def build_content_planning_pipeline() -> SequentialAgent:
    """Compose the full plan -> research -> draft/critique -> SEO sequence."""
    return SequentialAgent(
        name="content_planning_pipeline",
        description=(
            "The complete editorial pipeline: plans the post, researches it in "
            "parallel, drafts and critiques it iteratively, then scores it for SEO. "
            "Returns a reviewed draft ready for a publishing decision."
        ),
        sub_agents=[
            build_planner_agent(),
            build_research_team(),
            build_draft_revision_loop(),
            build_seo_reviewer_agent(),
        ],
    )


def build_publisher_agent() -> LlmAgent:
    """Build the publishing agent.

    The only agent holding the irreversible ``publish_post_to_cms`` tool, and the
    only one the guardrail plugin's allow-list authorises to call it. Routed to
    ``guardrail`` (Flash-Lite) because its job is gate-checking and relaying an
    approval request, not reasoning.
    """
    return LlmAgent(
        name="publisher_agent",
        model=resolve_model("guardrail"),
        generate_content_config=build_generate_content_config("guardrail"),
        description=(
            "Verifies the quality and SEO gates, then requests human approval to "
            "publish. The only agent authorised to publish."
        ),
        instruction=PUBLISHER_INSTRUCTION,
        tools=[*PUBLISHING_TOOLS, *SEO_TOOLS],
    )


def build_root_agent() -> LlmAgent:
    """Build the coordinator and the complete agent tree.

    Returns:
        The root :class:`~google.adk.agents.LlmAgent`, with the planning pipeline
        and the publisher attached as delegable sub-agents.
    """
    return LlmAgent(
        name="editorial_coordinator",
        model=resolve_model("planner"),
        generate_content_config=build_generate_content_config("planner"),
        description=(
            "Editorial coordinator for ContentForge. Talks to the author, and routes "
            "work to the planning pipeline and the publisher."
        ),
        # Applies to every agent in the tree, so the constitution cannot be
        # forgotten when a new sub-agent is added.
        global_instruction=GLOBAL_CONSTITUTION,
        instruction=COORDINATOR_INSTRUCTION,
        tools=list(MEMORY_TOOLS),
        sub_agents=[
            build_content_planning_pipeline(),
            build_publisher_agent(),
        ],
    )
