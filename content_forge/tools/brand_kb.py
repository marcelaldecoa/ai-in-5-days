"""Brand knowledge-base tools: style rules and published-post overlap detection.

Retrieval is backed by Vertex AI Search when
``CONTENTFORGE_VERTEX_SEARCH_DATASTORE`` is configured, and by a bundled local
corpus otherwise, so the agent is fully runnable without a GCP project. The
selection happens in :mod:`content_forge.memory.vector_store`; these tools are
agnostic to which backend answered.

Naming note: the tools are ``retrieve_brand_style_guide`` and
``search_published_posts_for_overlap`` rather than a single generic
``query_kb``. The model picks tools by name far more reliably when the name
states the *specific* job, and a narrow name also keeps each tool's schema
small enough to validate meaningfully.
"""

from __future__ import annotations

from typing import Any

from content_forge.errors import ErrorCode, tool_error, validate_arguments
from content_forge.memory.vector_store import get_knowledge_base
from content_forge.observability.logging_config import get_logger
from content_forge.schemas import (
    BrandStyleGuideRequest,
    PriorPostSearchResult,
    ToolStatus,
)

logger = get_logger(__name__)


def retrieve_brand_style_guide(topic: str, content_type: str = "blog_post") -> dict[str, Any]:
    """Retrieve the brand style rules that a post on this topic must satisfy.

    Call this **before** outlining or drafting. The returned rules are binding
    constraints, not suggestions: the critic agent checks the finished draft
    against this exact payload, so a draft written without first fetching it
    will usually fail review and force a costly revision loop.

    Args:
        topic: The subject of the post, e.g. ``"vector databases for RAG"``.
            Must be 3-300 characters. Used to select topic-specific overrides
            (for example, security topics carry extra claim-substantiation rules).
        content_type: Which content template applies. One of ``"blog_post"``,
            ``"tutorial"``, ``"case_study"`` or ``"announcement"``. Defaults to
            ``"blog_post"``. Determines the required section list and word budget.

    Returns:
        On success, a dict matching :class:`~content_forge.schemas.BrandStyleGuide`:

        * ``status`` - ``"ok"``.
        * ``tone`` - the approved voice, one of ``authoritative``,
          ``conversational``, ``technical``, ``playful``.
        * ``reading_level`` - target reading level, e.g. ``"grade 9-11"``.
        * ``banned_phrases`` - phrases the draft must not contain.
        * ``required_sections`` - headings the post must include, in order.
        * ``max_words`` - hard upper bound on draft length.
        * ``citation_policy`` - how factual claims must be attributed.

        On failure, a guided error envelope with ``status='error'``, an
        ``error_code``, and a ``recovery`` instruction describing what to do next.
    """
    request, error = validate_arguments(
        BrandStyleGuideRequest,
        "retrieve_brand_style_guide",
        topic=topic,
        content_type=content_type,
    )
    if error:
        return error
    assert request is not None

    try:
        guide = get_knowledge_base().fetch_style_guide(
            topic=request.topic, content_type=request.content_type
        )
    except Exception as exc:  # noqa: BLE001 - converted to guided error below
        logger.warning(
            "style_guide_lookup_failed", tool="retrieve_brand_style_guide", error=str(exc)
        )
        return tool_error(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "The brand knowledge base could not be reached.",
            recovery=(
                "Proceed using the conservative defaults: authoritative tone, "
                "grade 9-11 reading level, max 1600 words, one inline citation per "
                "factual claim. State clearly in your final output that the brand "
                "style guide was unavailable and the post needs a manual brand check."
            ),
        )

    logger.info(
        "style_guide_retrieved",
        tool="retrieve_brand_style_guide",
        topic=request.topic,
        content_type=request.content_type,
        required_sections=len(guide.required_sections),
    )
    return guide.model_dump(mode="json")


def search_published_posts_for_overlap(
    primary_keyword: str, max_results: int = 5
) -> dict[str, Any]:
    """Find already-published posts that would compete with this one in search.

    Publishing two posts targeting the same primary keyword splits their ranking
    signals and makes both rank worse - "keyword cannibalisation". Call this
    during planning, before committing to an angle. If ``cannibalisation_risk``
    comes back ``"high"``, change the angle or target a different keyword; do not
    proceed with the original plan.

    Args:
        primary_keyword: The keyword the new post intends to rank for, e.g.
            ``"rag evaluation metrics"``. Must be 2-80 characters.
        max_results: Maximum number of prior posts to return, 1-20. Defaults to 5.

    Returns:
        On success, a dict matching
        :class:`~content_forge.schemas.PriorPostSearchResult`:

        * ``status`` - ``"ok"``.
        * ``query`` - the keyword that was searched.
        * ``matches`` - list of prior posts, each with ``post_id``, ``title``,
          ``url``, ``published_on``, ``similarity`` (0.0-1.0), ``summary`` and
          ``primary_keyword``.
        * ``cannibalisation_risk`` - ``"none"``, ``"low"`` or ``"high"``.

        On failure, a guided error envelope with ``status='error'``, an
        ``error_code``, and a ``recovery`` instruction.
    """
    if not primary_keyword or not (2 <= len(primary_keyword) <= 80):
        return tool_error(
            ErrorCode.INVALID_ARGUMENTS,
            "primary_keyword must be a non-empty string of 2-80 characters.",
            recovery=(
                "Supply the single keyword phrase the post targets, e.g. "
                "'rag evaluation metrics'. Do not pass a full sentence or an "
                "empty string, and do not pass a list of keywords."
            ),
            field_errors=[{"field": "primary_keyword", "problem": "length out of range"}],
        )
    if not 1 <= max_results <= 20:
        return tool_error(
            ErrorCode.INVALID_ARGUMENTS,
            f"max_results must be between 1 and 20, got {max_results}.",
            recovery="Retry with max_results=5, which is sufficient for overlap checks.",
        )

    try:
        matches = get_knowledge_base().search_prior_posts(query=primary_keyword, limit=max_results)
    except Exception as exc:  # noqa: BLE001 - converted to guided error below
        logger.warning(
            "prior_post_search_failed",
            tool="search_published_posts_for_overlap",
            error=str(exc),
        )
        return tool_error(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "The published-post index could not be searched.",
            recovery=(
                "Continue planning, but flag in your final output that the "
                "cannibalisation check did not run and a human should verify the "
                "keyword is not already targeted before publishing."
            ),
        )

    # An exact keyword match on an existing post is the high-risk case; a merely
    # similar post is a soft warning the planner can weigh against its angle.
    exact = [m for m in matches if m.primary_keyword.lower() == primary_keyword.lower()]
    near = [m for m in matches if m.similarity >= 0.75]
    risk = "high" if exact else ("low" if near else "none")

    result = PriorPostSearchResult(
        status=ToolStatus.OK,
        query=primary_keyword,
        matches=matches,
        cannibalisation_risk=risk,
    )
    logger.info(
        "prior_posts_searched",
        tool="search_published_posts_for_overlap",
        query=primary_keyword,
        match_count=len(matches),
        cannibalisation_risk=risk,
    )
    return result.model_dump(mode="json")
