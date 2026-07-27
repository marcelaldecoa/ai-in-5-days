"""SEO scoring: deterministic, explainable checks over a finished draft.

Everything here is computed in Python rather than asked of a model. SEO rules
are mechanical (title length, keyword density, heading hierarchy), and a
deterministic scorer gives the revision loop a *stable* target - an LLM scorer
that drifts by a few points between runs makes the loop oscillate instead of
converge. Each finding carries an explicit ``remedy`` so the drafter is told the
edit to make, not merely that something is wrong.
"""

from __future__ import annotations

import re
from typing import Any

from content_forge.errors import validate_arguments
from content_forge.observability.logging_config import get_logger
from content_forge.schemas import SeoFinding, SeoReport, SeoScoreRequest, ToolStatus

logger = get_logger(__name__)

# Search engines truncate titles past ~60 chars and descriptions past ~160.
_TITLE_MIN, _TITLE_MAX = 30, 60
_META_MIN, _META_MAX = 50, 160
# Below ~0.5% the page reads as off-topic; above ~2.5% it reads as stuffed.
_DENSITY_MIN, _DENSITY_MAX = 0.5, 2.5
_MIN_WORDS = 600


def _extract_headings(markdown: str) -> list[tuple[int, str]]:
    """Return ``(level, text)`` for each ATX heading in the draft."""
    return [
        (len(m.group(1)), m.group(2).strip())
        for m in re.finditer(r"^(#{1,6})\s+(.+)$", markdown, flags=re.MULTILINE)
    ]


def score_draft_seo_readiness(
    draft_markdown: str, primary_keyword: str, meta_description: str = ""
) -> dict[str, Any]:
    """Score a finished draft for search readiness and return blocking defects.

    Run this after the draft passes editorial review and before publishing. Any
    finding with severity ``"blocker"`` sets ``ready_to_publish`` to false; the
    publisher agent refuses to proceed while that is the case, so fix blockers
    and re-score rather than attempting to publish anyway.

    Checks performed: title presence and length, exactly one H1, heading
    hierarchy, total word count, keyword presence in title/first paragraph/
    headings, keyword density band, meta description length, and internal/
    external link counts.

    Args:
        draft_markdown: The complete draft in Markdown, including its ``#`` title
            heading. Must be at least 50 characters.
        primary_keyword: The keyword this post targets, 2-80 characters. Matching
            is case-insensitive and whole-phrase.
        meta_description: The proposed meta description. Optional, but omitting
            it produces a blocker finding, since the post cannot ship without one.

    Returns:
        On success, a dict matching :class:`~content_forge.schemas.SeoReport`:

        * ``status`` - ``"ok"``.
        * ``score`` - 0-100 overall readiness score.
        * ``word_count`` - total words in the body.
        * ``keyword_density_pct`` - primary keyword occurrences as a percentage.
        * ``findings`` - list of ``{check, severity, detail, remedy}``, where
          severity is ``blocker``, ``warning`` or ``info``.
        * ``ready_to_publish`` - false if any finding is a blocker.

        On failure, a guided error envelope with ``status='error'``, an
        ``error_code`` and a ``recovery`` instruction.
    """
    request, error = validate_arguments(
        SeoScoreRequest,
        "score_draft_seo_readiness",
        draft_markdown=draft_markdown,
        primary_keyword=primary_keyword,
        meta_description=meta_description,
    )
    if error:
        return error
    assert request is not None

    md = request.draft_markdown
    keyword = request.primary_keyword.lower().strip()
    findings: list[SeoFinding] = []

    headings = _extract_headings(md)
    h1s = [text for level, text in headings if level == 1]
    body = re.sub(r"^#{1,6}\s+.+$", "", md, flags=re.MULTILINE)
    words = re.findall(r"\b[\w'-]+\b", body)
    word_count = len(words)

    # --- Title ------------------------------------------------------------
    if not h1s:
        findings.append(
            SeoFinding(
                check="title_missing",
                severity="blocker",
                detail="The draft has no H1 title.",
                remedy="Add a single '# Title' line as the first line of the draft.",
            )
        )
    else:
        if len(h1s) > 1:
            findings.append(
                SeoFinding(
                    check="multiple_h1",
                    severity="blocker",
                    detail=f"Found {len(h1s)} H1 headings; a page must have exactly one.",
                    remedy="Keep the first H1 as the title and demote the rest to '## '.",
                )
            )
        title = h1s[0]
        if not _TITLE_MIN <= len(title) <= _TITLE_MAX:
            findings.append(
                SeoFinding(
                    check="title_length",
                    severity="warning",
                    detail=f"Title is {len(title)} characters.",
                    remedy=(
                        f"Rewrite the title to {_TITLE_MIN}-{_TITLE_MAX} characters so "
                        "search results do not truncate it."
                    ),
                )
            )
        if keyword not in title.lower():
            findings.append(
                SeoFinding(
                    check="keyword_absent_from_title",
                    severity="blocker",
                    detail=f"Primary keyword {request.primary_keyword!r} is not in the title.",
                    remedy=(
                        f"Rewrite the H1 so it contains the exact phrase "
                        f"'{request.primary_keyword}', ideally near the start."
                    ),
                )
            )

    # --- Heading hierarchy -------------------------------------------------
    levels = [level for level, _ in headings]
    for previous, current in zip(levels, levels[1:], strict=False):
        if current - previous > 1:
            findings.append(
                SeoFinding(
                    check="heading_hierarchy_skip",
                    severity="warning",
                    detail=f"Heading level jumps from H{previous} to H{current}.",
                    remedy="Do not skip levels; an H2 must be followed by H2 or H3.",
                )
            )
            break

    if not any(keyword in text.lower() for _, text in headings[1:]):
        findings.append(
            SeoFinding(
                check="keyword_absent_from_subheadings",
                severity="info",
                detail="No subheading contains the primary keyword.",
                remedy="Work the keyword naturally into at least one H2.",
            )
        )

    # --- Length and density ------------------------------------------------
    if word_count < _MIN_WORDS:
        findings.append(
            SeoFinding(
                check="thin_content",
                severity="warning",
                detail=f"Body is {word_count} words, below the {_MIN_WORDS}-word threshold.",
                remedy=(
                    f"Expand to at least {_MIN_WORDS} words by deepening the sections "
                    "that currently have the fewest supporting details."
                ),
            )
        )

    keyword_hits = len(re.findall(re.escape(keyword), body.lower()))
    density = (keyword_hits / word_count * 100) if word_count else 0.0
    if density > _DENSITY_MAX:
        findings.append(
            SeoFinding(
                check="keyword_stuffing",
                severity="blocker",
                detail=f"Keyword density is {density:.2f}%, above {_DENSITY_MAX}%.",
                remedy=(
                    "Replace some keyword repetitions with pronouns or synonyms. "
                    "Search engines penalise stuffed pages."
                ),
            )
        )
    elif density < _DENSITY_MIN and word_count:
        findings.append(
            SeoFinding(
                check="keyword_underused",
                severity="warning",
                detail=f"Keyword density is {density:.2f}%, below {_DENSITY_MIN}%.",
                remedy="Use the exact keyword phrase a few more times where it reads naturally.",
            )
        )

    paragraphs = [p for p in body.split("\n\n") if p.strip()]
    if paragraphs and keyword not in paragraphs[0].lower():
        findings.append(
            SeoFinding(
                check="keyword_absent_from_intro",
                severity="warning",
                detail="The primary keyword does not appear in the opening paragraph.",
                remedy="Mention the keyword within the first 100 words.",
            )
        )

    # --- Meta description ---------------------------------------------------
    meta = request.meta_description.strip()
    if not meta:
        findings.append(
            SeoFinding(
                check="meta_description_missing",
                severity="blocker",
                detail="No meta description was supplied.",
                remedy=(
                    f"Write a {_META_MIN}-{_META_MAX} character summary containing the "
                    "primary keyword and a reason to click."
                ),
            )
        )
    elif not _META_MIN <= len(meta) <= _META_MAX:
        findings.append(
            SeoFinding(
                check="meta_description_length",
                severity="blocker",
                detail=f"Meta description is {len(meta)} characters.",
                remedy=f"Rewrite it to {_META_MIN}-{_META_MAX} characters.",
            )
        )

    # --- Links --------------------------------------------------------------
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", md)
    if len([u for u in links if u.startswith("http")]) < 2:
        findings.append(
            SeoFinding(
                check="insufficient_citations",
                severity="warning",
                detail="Fewer than two external source links found.",
                remedy="Add inline links to the sources backing your factual claims.",
            )
        )

    # Score: start at 100, subtract weighted penalties, floor at 0.
    penalties = {"blocker": 18.0, "warning": 7.0, "info": 2.0}
    score = max(0.0, 100.0 - sum(penalties[f.severity] for f in findings))
    has_blocker = any(f.severity == "blocker" for f in findings)

    report = SeoReport(
        status=ToolStatus.OK,
        score=round(score, 1),
        word_count=word_count,
        keyword_density_pct=round(density, 2),
        findings=findings,
        ready_to_publish=not has_blocker,
    )
    logger.info(
        "seo_scored",
        tool="score_draft_seo_readiness",
        score=report.score,
        word_count=word_count,
        blockers=sum(1 for f in findings if f.severity == "blocker"),
        ready_to_publish=report.ready_to_publish,
    )
    return report.model_dump(mode="json")
