"""Tool contract tests.

The invariant these protect: **a tool never raises and never returns an
unguided error.** A raise kills the turn; a bare error string leaves the model
with no next step. Both are agent-level failures that unit tests catch cheaply.
"""

from __future__ import annotations

from typing import Any

import pytest

from content_forge.errors import ErrorCode
from content_forge.tools.brand_kb import (
    retrieve_brand_style_guide,
    search_published_posts_for_overlap,
)
from content_forge.tools.publishing import (
    publish_post_to_cms,
    save_post_draft_for_human_review,
)
from content_forge.tools.research import (
    gather_supporting_evidence_for_subtopic,
    verify_claim_against_gathered_evidence,
)
from content_forge.tools.seo import score_draft_seo_readiness


class FakeToolContext:
    """Minimal ToolContext stand-in exposing only what the tools touch."""

    def __init__(self, *, confirmed: bool | None = None) -> None:
        self.state: dict[str, Any] = {}
        self.function_call_id = "test-fc-1"
        self.agent_name = "test_agent"
        self.invocation_id = "test-inv-1"
        self.requested_confirmations: list[dict[str, Any]] = []
        if confirmed is None:
            self.tool_confirmation = None
        else:
            self.tool_confirmation = type(
                "Confirmation", (), {"confirmed": confirmed, "payload": None, "hint": ""}
            )()

    def request_confirmation(self, *, hint: str = "", payload: Any = None) -> None:
        self.requested_confirmations.append({"hint": hint, "payload": payload})


def assert_guided_error(result: dict[str, Any]) -> None:
    """Assert a result is a well-formed guided error envelope."""
    assert result["status"] == "error", result
    for key in ("error_code", "message", "recovery", "retryable"):
        assert key in result, f"missing {key!r} in {result}"
    assert isinstance(result["retryable"], bool)
    assert len(result["recovery"]) > 20, "recovery must be actionable"


# ---------------------------------------------------------------------------
# Brand knowledge base
# ---------------------------------------------------------------------------


def test_style_guide_returns_binding_constraints():
    result = retrieve_brand_style_guide("vector databases for RAG", "tutorial")
    assert result["status"] == "ok"
    assert result["tone"] == "technical"
    assert "Prerequisites" in result["required_sections"]
    assert result["max_words"] == 2400
    assert result["banned_phrases"]


def test_style_guide_applies_topic_overlay():
    """A security topic must pick up the stricter overlay rules."""
    result = retrieve_brand_style_guide("encryption and compliance basics", "blog_post")
    assert result["status"] == "ok"
    assert "unhackable" in result["banned_phrases"]
    assert "primary source" in result["citation_policy"].lower()


def test_style_guide_rejects_unknown_content_type():
    result = retrieve_brand_style_guide("some topic", "newsletter")
    assert_guided_error(result)
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENTS.value
    assert "expected_schema" in result


def test_style_guide_rejects_short_topic():
    assert_guided_error(retrieve_brand_style_guide("ab"))


def test_overlap_search_flags_exact_keyword_collision():
    result = search_published_posts_for_overlap("model routing")
    assert result["status"] == "ok"
    assert result["cannibalisation_risk"] == "high"
    assert any(m["primary_keyword"] == "model routing" for m in result["matches"])


def test_overlap_search_reports_no_risk_for_novel_keyword():
    result = search_published_posts_for_overlap("underwater basket weaving robotics")
    assert result["status"] == "ok"
    assert result["cannibalisation_risk"] == "none"


@pytest.mark.parametrize(
    "keyword,limit", [("", 5), ("x", 5), ("valid keyword", 0), ("valid keyword", 99)]
)
def test_overlap_search_rejects_bad_arguments(keyword, limit):
    assert_guided_error(search_published_posts_for_overlap(keyword, limit))


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


def test_evidence_gathering_returns_sourced_claims():
    result = gather_supporting_evidence_for_subtopic("hybrid retrieval vs dense retrieval")
    assert result["status"] == "ok"
    assert result["evidence"], "expected evidence from the bundled corpus"
    for item in result["evidence"]:
        assert item["source_url"].startswith("http")
        assert item["credibility"] in {"primary", "reputable", "community", "unknown"}


def test_evidence_gathering_flags_weakly_sourced_claims():
    """Community-tier claims must surface in unsupported_angles, not silently pass."""
    result = gather_supporting_evidence_for_subtopic(
        "managed agent runtime adoption operational burden", desired_claim_count=5
    )
    assert result["status"] == "ok"
    weak = [e for e in result["evidence"] if e["credibility"] in {"community", "unknown"}]
    if weak:
        assert result["unsupported_angles"], "weak sources must be flagged"


def test_evidence_gathering_reports_empty_as_gap_not_error():
    """No evidence is a finding, not a failure - the drafter must be told."""
    result = gather_supporting_evidence_for_subtopic("zzzz qqqq xxxx nonexistent topic")
    assert result["status"] == "ok"
    assert result["evidence"] == []
    assert result["unsupported_angles"]


def test_evidence_gathering_rejects_vague_subtopic():
    assert_guided_error(gather_supporting_evidence_for_subtopic("AI"))


def test_claim_verification_detects_supported_and_unsupported():
    import json

    evidence = json.dumps(
        [
            {
                "claim": "Hybrid retrieval combining lexical matching with dense embeddings outperforms dense-only retrieval",
                "source_url": "https://arxiv.org/abs/2104.08663",
            }
        ]
    )
    supported = verify_claim_against_gathered_evidence(
        "Hybrid retrieval combining lexical matching with dense embeddings outperforms dense-only retrieval.",
        evidence,
    )
    assert supported["supported"] is True
    assert supported["best_match_url"].startswith("https://arxiv.org")

    unsupported = verify_claim_against_gathered_evidence(
        "Our product reduces inference costs by exactly seventy three percent.", evidence
    )
    assert unsupported["supported"] is False
    assert "no gathered evidence" in unsupported["advice"].lower()


def test_claim_verification_rejects_malformed_evidence():
    assert_guided_error(verify_claim_against_gathered_evidence("A claim.", "not json"))
    assert_guided_error(verify_claim_against_gathered_evidence("", "[]"))


# ---------------------------------------------------------------------------
# SEO
# ---------------------------------------------------------------------------


def _clean_draft() -> str:
    """A draft that satisfies every SEO check.

    Sized deliberately: one keyword mention per ~85-word paragraph keeps density
    near 1.2% (the band is 0.5-2.5%) and nine repetitions clear the 600-word
    minimum. A denser fixture would trip the keyword-stuffing blocker, which is
    the tool behaving correctly rather than a bug.
    """
    paragraph = (
        "Teams adopting llm agent observability hit the same wall: their traces show "
        "model calls but never the decisions behind them. This guide covers the span "
        "design that fixes it, the structured log fields that make those spans "
        "joinable, and the trace correlation that ties a user question to the final "
        "answer it produced. See the "
        "[OpenTelemetry spec](https://opentelemetry.io/docs/specs/otel/) and the "
        "[Cloud Logging docs](https://cloud.google.com/logging/docs/structured-logging) "
        "for the underlying primitives that keep this approach portable across "
        "vendors and runtimes. "
    )
    return (
        "# Practical llm agent observability for Teams\n\n"
        + paragraph
        + "\n\n## Why llm agent observability matters\n\n"
        + paragraph * 5
        + "\n\n## Key takeaways\n\n"
        + paragraph * 3
    )


def test_seo_passes_a_clean_draft():
    result = score_draft_seo_readiness(
        _clean_draft(),
        "llm agent observability",
        "A practical guide to llm agent observability: span design, structured logs and trace correlation.",
    )
    assert result["status"] == "ok"
    assert result["ready_to_publish"] is True
    assert not [f for f in result["findings"] if f["severity"] == "blocker"]


@pytest.mark.parametrize(
    "meta,expected_check",
    [
        ("", "meta_description_missing"),
        ("too short", "meta_description_length"),
    ],
)
def test_seo_blocks_on_bad_meta_description(meta, expected_check):
    result = score_draft_seo_readiness(_clean_draft(), "llm agent observability", meta)
    assert result["ready_to_publish"] is False
    assert expected_check in {f["check"] for f in result["findings"]}


def test_seo_blocks_when_keyword_missing_from_title():
    draft = _clean_draft().replace(
        "# Practical llm agent observability for Teams", "# Some Unrelated Title Entirely"
    )
    result = score_draft_seo_readiness(
        draft,
        "llm agent observability",
        "A practical guide to llm agent observability with span design and trace correlation.",
    )
    assert result["ready_to_publish"] is False
    assert "keyword_absent_from_title" in {f["check"] for f in result["findings"]}


def test_seo_blocks_on_multiple_h1():
    draft = _clean_draft() + "\n\n# A Second llm agent observability Title\n\nMore text here."
    result = score_draft_seo_readiness(
        draft,
        "llm agent observability",
        "A practical guide to llm agent observability with span design and trace correlation.",
    )
    assert "multiple_h1" in {f["check"] for f in result["findings"]}
    assert result["ready_to_publish"] is False


def test_seo_findings_all_carry_a_remedy():
    """A finding without a fix is not actionable for the drafter."""
    poor_draft = (
        "# A Title That Does Not Mention The Target Phrase\n\n"
        "This body is long enough to clear the minimum input length but is "
        "otherwise a thin, unsourced draft with no links and no meta description."
    )
    result = score_draft_seo_readiness(poor_draft, "missing keyword", "")
    assert result["status"] == "ok", result
    assert result["findings"], "a poor draft must produce findings"
    for finding in result["findings"]:
        assert finding["remedy"], f"finding {finding['check']} has no remedy"


def test_seo_rejects_undersized_draft():
    assert_guided_error(score_draft_seo_readiness("tiny", "keyword", ""))


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


VALID_PUBLISH = {
    "title": "A Perfectly Reasonable Post Title Here",
    "body_markdown": "x" * 500,
    "primary_keyword": "test keyword",
    "meta_description": "y" * 100,
    "tags": ["testing"],
    "author_email": "author@example.com",
}


def test_publish_requires_confirmation_on_first_call():
    ctx = FakeToolContext()
    result = publish_post_to_cms(**VALID_PUBLISH, tool_context=ctx)

    assert result["status"] == "needs_confirmation"
    assert result["post_id"] == "", "nothing may be published before approval"
    assert result["url"] == ""
    assert ctx.requested_confirmations, "the tool must raise a confirmation request"
    hint = ctx.requested_confirmations[0]["hint"]
    assert "irreversible" in hint.lower()
    assert VALID_PUBLISH["title"] in hint


def test_publish_confirmation_payload_lets_a_human_decide():
    """The approval summary must contain what a reviewer needs, without scrolling."""
    ctx = FakeToolContext()
    publish_post_to_cms(**VALID_PUBLISH, tool_context=ctx)
    payload = ctx.requested_confirmations[0]["payload"]
    for key in ("title", "slug", "word_count", "visibility", "irreversible", "scheduled_for"):
        assert key in payload, f"approval payload missing {key!r}"
    assert payload["irreversible"] is True


def test_publish_rejected_by_human_is_terminal():
    ctx = FakeToolContext(confirmed=False)
    result = publish_post_to_cms(**VALID_PUBLISH, tool_context=ctx)

    assert_guided_error(result)
    assert result["error_code"] == ErrorCode.PERMISSION_DENIED.value
    assert result["retryable"] is False
    assert "do not retry" in result["recovery"].lower()


def test_publish_proceeds_once_confirmed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = FakeToolContext(confirmed=True)
    result = publish_post_to_cms(**VALID_PUBLISH, tool_context=ctx)

    assert result["status"] == "ok"
    assert result["post_id"]
    assert result["published_at"]
    # With no CMS credential configured the write must be recorded honestly as
    # simulated, not passed off as a live publication.
    assert "note" in result
    assert "NOT published" in result["note"]


def test_publish_validates_before_asking_for_confirmation():
    """A malformed publish must fail validation, not waste a human's attention."""
    ctx = FakeToolContext()
    result = publish_post_to_cms(
        **{**VALID_PUBLISH, "title": "short", "meta_description": "tiny"},
        tool_context=ctx,
    )
    assert_guided_error(result)
    assert not ctx.requested_confirmations, "must not prompt a human for an invalid request"


def test_save_draft_needs_no_confirmation():
    """The reversible counterpart is deliberately ungated."""
    ctx = FakeToolContext()
    result = save_post_draft_for_human_review("A Draft Title", "x" * 100, ctx)

    assert result["status"] == "ok"
    assert result["draft_id"]
    assert not ctx.requested_confirmations
    assert ctx.state["last_saved_draft"]["title"] == "A Draft Title"


def test_save_draft_rejects_empty_body():
    assert_guided_error(save_post_draft_for_human_review("Title", "tiny", FakeToolContext()))
