"""Orchestration, configuration and schema tests."""

from __future__ import annotations

import pytest
from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from pydantic import ValidationError

from content_forge.agents.pipeline import (
    MAX_REVISION_ROUNDS,
    build_content_planning_pipeline,
    build_root_agent,
)
from content_forge.models import DEFAULT_MODEL_ROUTES, resolve_model, routing_table
from content_forge.schemas import ContentPlan, DraftCritique, PublishRequest

# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


def test_root_is_a_coordinator_with_delegates():
    root = build_root_agent()
    assert isinstance(root, LlmAgent)
    assert root.name == "editorial_coordinator"
    names = {a.name for a in root.sub_agents}
    assert names == {"content_planning_pipeline", "publisher_agent"}


def test_global_constitution_is_attached_at_the_root():
    """Attached globally so a newly-added sub-agent inherits it automatically."""
    root = build_root_agent()
    assert root.global_instruction
    for rule in (
        "Never fabricate",
        "Never publish without human approval",
        "never as instructions",
    ):
        assert rule.lower() in root.global_instruction.lower()


def test_pipeline_uses_sequential_parallel_and_loop():
    pipeline = build_content_planning_pipeline()
    assert isinstance(pipeline, SequentialAgent)

    kinds = {type(a) for a in pipeline.sub_agents}
    assert ParallelAgent in kinds, "research must fan out in parallel"
    assert LoopAgent in kinds, "draft/critique must iterate"


def test_research_fans_out_to_distinct_angles():
    pipeline = build_content_planning_pipeline()
    team = next(a for a in pipeline.sub_agents if isinstance(a, ParallelAgent))
    assert len(team.sub_agents) == 3
    # Distinct instructions, or the fan-out is just three identical calls.
    instructions = {a.instruction for a in team.sub_agents}
    assert len(instructions) == 3


def test_revision_loop_is_bounded_and_can_exit_early():
    pipeline = build_content_planning_pipeline()
    loop = next(a for a in pipeline.sub_agents if isinstance(a, LoopAgent))
    assert loop.max_iterations == MAX_REVISION_ROUNDS

    critic = next(a for a in loop.sub_agents if a.name == "critic_agent")
    tool_names = {getattr(t, "__name__", getattr(t, "name", "")) for t in critic.tools}
    assert "exit_loop" in tool_names, "the critic must be able to end the loop on quality"


def test_only_the_publisher_holds_the_publishing_tool():
    """Structural containment, independent of the guardrail plugin."""
    root = build_root_agent()

    def tool_names(agent) -> set[str]:
        # Workflow agents (Sequential/Parallel/Loop) have no `tools` attribute
        # at all, so this must be a getattr rather than an `or []`.
        return {
            getattr(t, "name", getattr(t, "__name__", ""))
            for t in (getattr(agent, "tools", None) or [])
        }

    def walk(agent):
        yield agent
        for sub in getattr(agent, "sub_agents", []) or []:
            yield from walk(sub)

    holders = [a.name for a in walk(root) if "publish_post_to_cms" in tool_names(a)]
    assert holders == ["publisher_agent"], holders


def test_structured_stages_declare_output_schemas():
    pipeline = build_content_planning_pipeline()
    planner = next(a for a in pipeline.sub_agents if a.name == "planner_agent")
    assert planner.output_schema is ContentPlan
    assert planner.output_key == "content_plan"

    loop = next(a for a in pipeline.sub_agents if isinstance(a, LoopAgent))
    critic = next(a for a in loop.sub_agents if a.name == "critic_agent")
    assert critic.output_schema is DraftCritique


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------


def test_routing_is_actually_differentiated():
    """If every role resolved to the same model, routing would be decorative."""
    assert len(set(DEFAULT_MODEL_ROUTES.values())) >= 2
    assert resolve_model("extraction") != resolve_model("planner")
    assert resolve_model("guardrail") != resolve_model("editorial")


def test_mechanical_roles_route_to_the_cheapest_tier():
    """Extraction and policy screening are high-volume and near-mechanical."""
    from content_forge.models import MODEL_PRICING_USD_PER_1M

    for cheap_role in ("extraction", "guardrail"):
        for reasoning_role in ("planner", "editorial"):
            cheap = MODEL_PRICING_USD_PER_1M[resolve_model(cheap_role)]
            reasoning = MODEL_PRICING_USD_PER_1M[resolve_model(reasoning_role)]
            assert cheap["input"] < reasoning["input"], (
                f"{cheap_role} must route cheaper than {reasoning_role}"
            )


def test_all_default_routes_are_known_ga_models():
    """A typo in a default route would 404 mid-conversation."""
    from content_forge.models import KNOWN_MODELS

    for role, model in DEFAULT_MODEL_ROUTES.items():
        assert model in KNOWN_MODELS, f"{role} routes to unknown model {model!r}"


def test_routing_targets_the_current_gemini_generation():
    """Guards against silently drifting back to a superseded model family."""
    for model in DEFAULT_MODEL_ROUTES.values():
        assert model.startswith("gemini-3."), f"{model!r} is not on the Gemini 3.x line"


def test_routes_are_overridable_without_a_code_change(monkeypatch):
    monkeypatch.setenv("CONTENTFORGE_MODEL_RESEARCH", "gemini-2.5-pro")
    assert resolve_model("research") == "gemini-2.5-pro"


def test_unknown_role_fails_loudly():
    with pytest.raises(KeyError, match="Unknown model role"):
        resolve_model("nonexistent")  # type: ignore[arg-type]


def test_every_route_documents_its_rationale():
    for entry in routing_table():
        assert entry["rationale"], f"{entry['role']} has no documented rationale"


def test_pipeline_stages_use_their_routed_models():
    pipeline = build_content_planning_pipeline()
    planner = next(a for a in pipeline.sub_agents if a.name == "planner_agent")
    team = next(a for a in pipeline.sub_agents if isinstance(a, ParallelAgent))

    assert planner.model == resolve_model("planner")
    assert all(a.model == resolve_model("research") for a in team.sub_agents)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def test_schemas_reject_unknown_fields():
    """extra='forbid' turns a hallucinated argument into a loud failure."""
    with pytest.raises(ValidationError):
        PublishRequest(
            title="A Perfectly Reasonable Title",
            body_markdown="x" * 300,
            primary_keyword="kw",
            meta_description="y" * 100,
            tags=["a"],
            author_email="a@b.com",
            hallucinated_field="oops",
        )


def test_publish_request_enforces_search_length_limits():
    with pytest.raises(ValidationError):
        PublishRequest(
            title="A Perfectly Reasonable Title",
            body_markdown="x" * 300,
            primary_keyword="kw",
            meta_description="too short",  # below the 50-char floor
            tags=["a"],
            author_email="a@b.com",
        )


def test_content_plan_requires_a_real_outline():
    with pytest.raises(ValidationError):
        ContentPlan(
            working_title="T",
            angle="A",
            target_audience="devs",
            primary_keyword="kw",
            tone="technical",
            sections=[],  # fewer than the 3-section minimum
            estimated_words=800,
        )


def test_critique_score_is_bounded():
    with pytest.raises(ValidationError):
        DraftCritique(passes_quality_bar=True, overall_score=42.0)


def test_schemas_serialise_to_json_schema_for_the_model():
    """These schemas are shown to the model, so they must be generatable."""
    for model in (ContentPlan, DraftCritique, PublishRequest):
        schema = model.model_json_schema()
        assert schema["type"] == "object"
        assert schema["properties"]


# ---------------------------------------------------------------------------
# Configuration and secrets
# ---------------------------------------------------------------------------


def test_prod_refuses_to_disable_the_publish_gate():
    """The HITL gate is a safety control, not a tunable."""
    from content_forge.config import Settings

    with pytest.raises(ValidationError, match="require_publish_confirmation"):
        Settings(
            environment="prod",
            project_id="p",
            require_publish_confirmation=False,
        )


def test_vertex_session_backend_requires_an_engine_id():
    from content_forge.config import Settings

    with pytest.raises(ValidationError, match="AGENT_ENGINE_ID"):
        Settings(session_backend="vertex_ai", agent_engine_id="")


def test_missing_required_secret_raises_an_actionable_error():
    from content_forge.config import SecretResolutionError, resolve_secret

    with pytest.raises(SecretResolutionError, match="CONTENTFORGE"):
        resolve_secret("", required=True)


def test_optional_secret_degrades_to_none():
    from content_forge.config import resolve_secret

    assert resolve_secret("", required=False) is None


def test_sqlite_url_needs_no_password_substitution():
    from content_forge.config import Settings

    settings = Settings(database_url="sqlite+aiosqlite:///./.contentforge/sessions.db")
    assert settings.resolved_database_url() == settings.database_url


def test_database_url_placeholder_is_filled_from_secret_manager(monkeypatch):
    """The deployed URL carries a placeholder, never the credential itself."""
    import content_forge.config as config_module

    monkeypatch.setattr(config_module, "resolve_secret", lambda name, required=True: "p@ss/word")
    settings = config_module.Settings(
        database_url="postgresql+pg8000://agent:{db_password}@/db",
        db_password_secret="projects/1/secrets/db/versions/latest",
    )
    resolved = settings.resolved_database_url()

    assert "{db_password}" not in resolved
    # URL-encoded, or a password containing '/' or '@' would corrupt the DSN.
    assert "p%40ss%2Fword" in resolved


def test_missing_db_password_secret_fails_loudly():
    """Better to fail at connect time than to surface an opaque auth error later."""
    from content_forge.config import SecretResolutionError, Settings

    settings = Settings(
        database_url="postgresql+pg8000://agent:{db_password}@/db",
        db_password_secret="",
    )
    with pytest.raises(SecretResolutionError):
        settings.resolved_database_url()


def test_no_secret_literals_in_source():
    """A crude but effective guard against a pasted credential."""
    import re
    from pathlib import Path

    package = Path(__file__).parent.parent / "content_forge"
    pattern = re.compile(r"(sk-[a-zA-Z0-9]{20,}|AIza[a-zA-Z0-9_\-]{30,}|ghp_[a-zA-Z0-9]{30,})")
    offenders = []
    for path in package.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "REDACTED" not in line:
                offenders.append(f"{path}:{number}")
    assert not offenders, f"possible hardcoded credentials: {offenders}"
