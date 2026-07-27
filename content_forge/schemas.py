"""Explicit input/output schemas for every tool and structured agent.

Why these exist
---------------
Free-form dict passing between an LLM and a tool is the most common source of
silent agent failure: the model invents a field, the tool reads ``None``, and
the error surfaces three steps later as nonsense output. Every schema in this
module is used in one of two enforcing positions:

* **Tool inputs** - validated inside the tool via
  :func:`content_forge.errors.validated_call`, which converts a Pydantic
  ``ValidationError`` into a *guided* error envelope the LLM can act on.
* **Agent outputs** - attached to :class:`~google.adk.agents.LlmAgent` as
  ``output_schema``, which constrains decoding so the model physically cannot
  emit a malformed object.

``model_config = ConfigDict(extra="forbid")`` is deliberate: it makes a
hallucinated argument a loud validation failure instead of a silently dropped
field. ``json_schema_extra`` examples are included because they are rendered
into the JSON Schema the model actually sees, which measurably reduces
malformed calls.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base for all schemas: unknown fields are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------


class ToolStatus(str, Enum):
    """Uniform outcome discriminator present on every tool response."""

    OK = "ok"
    ERROR = "error"
    NEEDS_CONFIRMATION = "needs_confirmation"


class ContentTone(str, Enum):
    """Permitted brand voices. Constrains the drafter to an approved register."""

    AUTHORITATIVE = "authoritative"
    CONVERSATIONAL = "conversational"
    TECHNICAL = "technical"
    PLAYFUL = "playful"


class SourceCredibility(str, Enum):
    """Coarse trust tier assigned to a research source."""

    PRIMARY = "primary"  # first-party docs, standards bodies, filings
    REPUTABLE = "reputable"  # established press, peer-reviewed work
    COMMUNITY = "community"  # forums, blogs, social - corroboration required
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Brand knowledge base
# ---------------------------------------------------------------------------


class BrandStyleGuideRequest(StrictModel):
    """Arguments for :func:`retrieve_brand_style_guide`."""

    topic: str = Field(
        min_length=3,
        max_length=300,
        description="The post topic, used to retrieve topic-specific style rules.",
    )
    content_type: Literal["blog_post", "tutorial", "case_study", "announcement"] = Field(
        default="blog_post",
        description="Which content template's rules to retrieve.",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"topic": "vector databases for RAG", "content_type": "tutorial"}]
        },
    )


class BrandStyleGuide(StrictModel):
    """The subset of brand rules relevant to one post."""

    status: ToolStatus = ToolStatus.OK
    tone: ContentTone = Field(description="Approved voice for this content type.")
    reading_level: str = Field(description="Target reading level, e.g. 'grade 9-11'.")
    banned_phrases: list[str] = Field(
        default_factory=list,
        description="Phrases the draft must not contain (legal or brand risk).",
    )
    required_sections: list[str] = Field(
        default_factory=list,
        description="Section headings this content type must include, in order.",
    )
    max_words: int = Field(default=1600, ge=200, le=6000)
    citation_policy: str = Field(
        description="How claims must be attributed, e.g. 'inline link per factual claim'."
    )


class PriorPostMatch(StrictModel):
    """One previously-published post retrieved from the corpus."""

    post_id: str
    title: str
    url: str
    published_on: str = Field(description="ISO-8601 date, e.g. '2025-02-14'.")
    similarity: float = Field(ge=0.0, le=1.0, description="Cosine similarity to the query.")
    summary: str
    primary_keyword: str


class PriorPostSearchResult(StrictModel):
    """Response of :func:`search_published_posts_for_overlap`."""

    status: ToolStatus = ToolStatus.OK
    query: str
    matches: list[PriorPostMatch] = Field(default_factory=list)
    cannibalisation_risk: Literal["none", "low", "high"] = Field(
        description=(
            "'high' when an existing post already targets this keyword, meaning the "
            "new post would compete with it in search rankings and the angle must change."
        )
    )


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


class EvidenceSnippet(StrictModel):
    """A single citable claim with its provenance."""

    claim: str = Field(description="The factual assertion, stated in one sentence.")
    source_url: str
    source_title: str
    credibility: SourceCredibility
    published_on: str | None = Field(
        default=None, description="ISO-8601 date, or null when the source is undated."
    )


class ResearchBundle(StrictModel):
    """Response of :func:`gather_supporting_evidence_for_claim`."""

    status: ToolStatus = ToolStatus.OK
    subtopic: str
    evidence: list[EvidenceSnippet] = Field(default_factory=list)
    unsupported_angles: list[str] = Field(
        default_factory=list,
        description=(
            "Angles for which no credible source was found. The drafter MUST NOT "
            "assert these; it should either omit them or mark them as opinion."
        ),
    )


# ---------------------------------------------------------------------------
# Planning / drafting (used as agent output_schema)
# ---------------------------------------------------------------------------


class OutlineSection(StrictModel):
    """One planned section of the post."""

    heading: str
    intent: str = Field(description="What this section must accomplish for the reader.")
    talking_points: list[str] = Field(min_length=1)
    supporting_claim_ids: list[int] = Field(
        default_factory=list,
        description="Indices into the research evidence list backing this section.",
    )


class ContentPlan(StrictModel):
    """Structured editorial plan. Attached as ``output_schema`` to the planner."""

    working_title: str = Field(max_length=120)
    angle: str = Field(description="The specific thesis that differentiates this post.")
    target_audience: str
    primary_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list, max_length=8)
    tone: ContentTone
    sections: list[OutlineSection] = Field(min_length=3)
    estimated_words: int = Field(ge=200, le=6000)


class DraftCritique(StrictModel):
    """Self-evaluation emitted by the critic agent.

    Used both as a quality gate and as the loop-termination signal for the
    draft/revise :class:`~google.adk.agents.LoopAgent`.
    """

    passes_quality_bar: bool = Field(
        description="True only when every blocking issue list below is empty."
    )
    factual_issues: list[str] = Field(
        default_factory=list,
        description="Claims in the draft not supported by the gathered evidence.",
    )
    brand_violations: list[str] = Field(
        default_factory=list,
        description="Breaches of tone, banned phrases, or required sections.",
    )
    structural_issues: list[str] = Field(default_factory=list)
    overall_score: float = Field(ge=0.0, le=10.0)
    revision_instructions: str = Field(
        default="",
        description="Concrete, actionable edits for the drafter. Empty when passing.",
    )


# ---------------------------------------------------------------------------
# SEO
# ---------------------------------------------------------------------------


class SeoScoreRequest(StrictModel):
    """Arguments for :func:`score_draft_seo_readiness`."""

    draft_markdown: str = Field(min_length=50, description="The full draft in Markdown.")
    primary_keyword: str = Field(min_length=2, max_length=80)
    meta_description: str = Field(
        default="", max_length=400, description="Proposed meta description, if any."
    )


class SeoFinding(StrictModel):
    """One actionable SEO defect."""

    check: str = Field(description="Machine-readable check id, e.g. 'title_length'.")
    severity: Literal["blocker", "warning", "info"]
    detail: str
    remedy: str = Field(description="The specific edit that resolves this finding.")


class SeoReport(StrictModel):
    """Response of :func:`score_draft_seo_readiness`."""

    status: ToolStatus = ToolStatus.OK
    score: float = Field(ge=0.0, le=100.0)
    word_count: int
    keyword_density_pct: float
    findings: list[SeoFinding] = Field(default_factory=list)
    ready_to_publish: bool = Field(description="False when any finding has severity 'blocker'.")


# ---------------------------------------------------------------------------
# Publishing (high-stakes, human-gated)
# ---------------------------------------------------------------------------


class PublishRequest(StrictModel):
    """Arguments for :func:`publish_post_to_cms`.

    Every field is required: publishing is irreversible and externally visible,
    so the schema refuses to let the model paper over a missing value.
    """

    title: str = Field(min_length=10, max_length=120)
    body_markdown: str = Field(min_length=200)
    primary_keyword: str = Field(min_length=2)
    meta_description: str = Field(min_length=50, max_length=160)
    tags: list[str] = Field(min_length=1, max_length=10)
    author_email: str = Field(description="Owning author; receives the publish receipt.")
    scheduled_for: str | None = Field(
        default=None,
        description="ISO-8601 timestamp to schedule for, or null to publish immediately.",
    )


class PublishReceipt(StrictModel):
    """Response of :func:`publish_post_to_cms`."""

    status: ToolStatus
    post_id: str = ""
    url: str = ""
    published_at: str = ""
    confirmation_required_reason: str = Field(
        default="",
        description="Populated when status is 'needs_confirmation'.",
    )
