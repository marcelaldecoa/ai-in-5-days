"""Guards that the architecture diagram stays truthful.

A diagram in a README is documentation that nothing executes, so it rots
silently: an agent gets renamed or a stage is added and the picture quietly
starts lying. These tests tie the Mermaid source to the actual agent tree, so
the drift shows up as a test failure rather than as a reviewer's confusion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from content_forge.agents.pipeline import build_root_agent
from content_forge.models import DEFAULT_MODEL_ROUTES

README = Path(__file__).parent.parent / "README.md"


def _mermaid_blocks() -> list[str]:
    """Every ```mermaid fence in the README."""
    return re.findall(r"```mermaid\n(.*?)```", README.read_text(encoding="utf-8"), re.DOTALL)


def _diagram() -> str:
    blocks = _mermaid_blocks()
    assert blocks, "the README has no ```mermaid architecture diagram"
    return blocks[0]


def _agent_names() -> set[str]:
    """Every agent name in the real tree."""

    def walk(agent):
        yield agent.name
        for sub in getattr(agent, "sub_agents", []) or []:
            yield from walk(sub)

    return set(walk(build_root_agent()))


def test_readme_has_a_mermaid_diagram():
    assert _mermaid_blocks(), "architecture diagram is missing or is not a mermaid fence"


def test_diagram_is_a_flowchart():
    assert _diagram().lstrip().startswith("flowchart"), (
        "the diagram should declare a flowchart type on its first line"
    )


@pytest.mark.parametrize(
    "agent_name",
    [
        "editorial_coordinator",
        "content_planning_pipeline",
        "planner_agent",
        "parallel_research_team",
        "draft_revision_loop",
        "drafter_agent",
        "critic_agent",
        "seo_reviewer_agent",
        "publisher_agent",
    ],
)
def test_diagram_names_match_the_real_agent_tree(agent_name):
    """Every agent the diagram claims exists must actually exist."""
    assert agent_name in _agent_names(), f"{agent_name} is not in the built agent tree"
    assert agent_name in _diagram(), f"{agent_name} is missing from the README diagram"


def test_diagram_shows_the_parallel_fan_out():
    """The fan-out is the point of that stage; one edge would misrepresent it."""
    diagram = _diagram()
    fan_out = re.findall(r"planner\s*-->\s*r\d", diagram)
    assert len(fan_out) == 3, (
        f"expected 3 fan-out edges from the planner, found {len(fan_out)} - "
        "the research stage runs three concurrent angles"
    )


def test_diagram_shows_the_revision_loop_as_a_cycle():
    """A one-way arrow would make the LoopAgent look like another sequential step."""
    diagram = _diagram()
    assert re.search(r"drafter\s*-->\s*critic", diagram), "missing drafter -> critic edge"
    assert re.search(r"critic\s*-->\|.*?\|\s*drafter", diagram), (
        "missing the critic -> drafter revision edge, so the loop does not read as a loop"
    )


def test_diagram_marks_the_human_gate():
    """The publish gate is the system's most important control."""
    diagram = _diagram()
    assert "gate" in diagram
    assert "Human approval" in diagram
    assert "irreversible" in diagram.lower()


def test_diagram_shows_the_publisher_as_sole_holder_of_the_publish_tool():
    diagram = _diagram()
    assert "publish_post_to_cms" in diagram
    assert "sole holder" in diagram, "the containment property should be stated on the node"


def test_diagram_model_names_are_currently_routed_models():
    """Catches the diagram still advertising a model the code no longer uses."""
    routed = set(DEFAULT_MODEL_ROUTES.values())
    mentioned = set(re.findall(r"gemini-[0-9.]+-[a-z-]+", _diagram()))
    assert mentioned, "the diagram no longer names any model"
    stale = mentioned - routed
    assert not stale, f"diagram names models that are not in the routing table: {sorted(stale)}"


def test_diagram_pins_node_colours_for_dark_mode():
    """Theme-derived fills can render dark-on-dark in GitHub's dark theme."""
    diagram = _diagram()
    class_defs = re.findall(r"classDef\s+\w+\s+([^\n]+)", diagram)
    assert class_defs, "no classDef styling found"
    for definition in class_defs:
        assert "fill:" in definition and "color:" in definition, (
            f"classDef pins a fill without pinning text colour: {definition!r}"
        )
