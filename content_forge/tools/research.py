"""Research tools: evidence gathering with explicit provenance and credibility.

The single most damaging failure mode for an automated blog writer is a
confident, unsourced, wrong claim. These tools are therefore built so that the
*absence* of evidence is as loud as its presence: every response carries an
``unsupported_angles`` list, and the drafter's constitution forbids asserting
anything that appears there.
"""

from __future__ import annotations

from typing import Any

from content_forge.errors import ErrorCode, tool_error
from content_forge.memory.vector_store import get_knowledge_base
from content_forge.observability.logging_config import get_logger
from content_forge.schemas import ResearchBundle, ToolStatus

logger = get_logger(__name__)

#: Credibility tiers that may be cited without corroboration.
_SELF_SUFFICIENT_TIERS = {"primary", "reputable"}


def gather_supporting_evidence_for_subtopic(
    subtopic: str, desired_claim_count: int = 4
) -> dict[str, Any]:
    """Gather citable evidence for one subtopic, with source URLs and credibility.

    Call this once per outline section that makes a factual claim. Run several
    calls concurrently across subtopics rather than one broad call - narrow
    queries return materially better-attributed evidence.

    Every returned claim carries a ``credibility`` tier. Claims tiered
    ``community`` or ``unknown`` must be corroborated by a second independent
    source before you assert them, or attributed explicitly as opinion.

    Args:
        subtopic: A single, specific research question, e.g.
            ``"typical recall@10 for hybrid search vs dense-only"``. Broad topics
            such as ``"AI"`` return low-quality evidence; be specific.
        desired_claim_count: How many distinct claims to aim for, 1-10.
            Defaults to 4. This is a target, not a guarantee - fewer are returned
            when credible sources do not exist, which is itself signal.

    Returns:
        On success, a dict matching :class:`~content_forge.schemas.ResearchBundle`:

        * ``status`` - ``"ok"``.
        * ``subtopic`` - the question that was researched.
        * ``evidence`` - list of claims, each with ``claim``, ``source_url``,
          ``source_title``, ``credibility`` (``primary``/``reputable``/
          ``community``/``unknown``) and ``published_on``.
        * ``unsupported_angles`` - angles for which no credible source was found.
          **You must not assert these as fact.**

        On failure, a guided error envelope with ``status='error'``, an
        ``error_code`` and a ``recovery`` instruction.
    """
    if not subtopic or len(subtopic.strip()) < 5:
        return tool_error(
            ErrorCode.INVALID_ARGUMENTS,
            "subtopic must be a specific research question of at least 5 characters.",
            recovery=(
                "Rephrase as a narrow, answerable question. Instead of 'AI', ask "
                "'what recall@10 do hybrid retrievers achieve on BEIR'."
            ),
        )
    if not 1 <= desired_claim_count <= 10:
        return tool_error(
            ErrorCode.INVALID_ARGUMENTS,
            f"desired_claim_count must be between 1 and 10, got {desired_claim_count}.",
            recovery="Retry with desired_claim_count=4, a good default per section.",
        )

    try:
        evidence = get_knowledge_base().gather_evidence(
            subtopic=subtopic, limit=desired_claim_count
        )
    except Exception as exc:  # noqa: BLE001 - converted to guided error below
        logger.warning("evidence_gathering_failed", tool="gather_evidence", error=str(exc))
        return tool_error(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            f"The research index could not be reached for subtopic {subtopic!r}.",
            recovery=(
                "Do not fabricate citations to fill the gap. Either omit this "
                "section, or write it without factual claims and note that it "
                "needs sourcing before publication."
            ),
        )

    if not evidence:
        # Not an error - a genuine, actionable finding the drafter must respect.
        logger.info("evidence_empty", tool="gather_evidence", subtopic=subtopic)
        return ResearchBundle(
            status=ToolStatus.OK,
            subtopic=subtopic,
            evidence=[],
            unsupported_angles=[subtopic],
        ).model_dump(mode="json")

    weak = [e.claim for e in evidence if e.credibility not in _SELF_SUFFICIENT_TIERS]
    bundle = ResearchBundle(
        status=ToolStatus.OK,
        subtopic=subtopic,
        evidence=evidence,
        unsupported_angles=weak,
    )
    logger.info(
        "evidence_gathered",
        tool="gather_evidence",
        subtopic=subtopic,
        claim_count=len(evidence),
        weakly_sourced=len(weak),
    )
    return bundle.model_dump(mode="json")


def verify_claim_against_gathered_evidence(claim: str, evidence_json: str) -> dict[str, Any]:
    """Check whether a specific draft sentence is actually supported by the evidence.

    Used by the fact-checking stage to catch the classic failure where a draft
    subtly overstates a source - turning "improved recall in one benchmark" into
    "always improves recall". Deterministic string/entity overlap only; it is a
    cheap pre-filter that flags candidates for the critic agent to judge, not a
    replacement for that judgement.

    Args:
        claim: The exact sentence from the draft to verify.
        evidence_json: JSON array of evidence objects as returned in the
            ``evidence`` field of :func:`gather_supporting_evidence_for_subtopic`.

    Returns:
        On success, a dict with:

        * ``status`` - ``"ok"``.
        * ``claim`` - the sentence that was checked.
        * ``supported`` - bool, whether any evidence item plausibly backs it.
        * ``best_match_url`` - the URL of the closest supporting source, or ``""``.
        * ``overlap_score`` - 0.0-1.0 lexical overlap with the best match.
        * ``advice`` - what to do about it.

        On failure, a guided error envelope.
    """
    import json

    if not claim.strip():
        return tool_error(
            ErrorCode.INVALID_ARGUMENTS,
            "claim must be a non-empty sentence from the draft.",
            recovery="Pass the exact sentence you want to verify, copied from the draft.",
        )
    try:
        evidence = json.loads(evidence_json)
        if not isinstance(evidence, list):
            raise ValueError("expected a JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        return tool_error(
            ErrorCode.INVALID_ARGUMENTS,
            f"evidence_json could not be parsed as a JSON array: {exc}",
            recovery=(
                "Pass the 'evidence' array from a previous "
                "gather_supporting_evidence_for_subtopic response, serialised with "
                "json.dumps. Do not pass the whole response object."
            ),
        )

    claim_terms = {w for w in claim.lower().split() if len(w) > 4}
    best_score, best_url = 0.0, ""
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source_terms = {w for w in str(item.get("claim", "")).lower().split() if len(w) > 4}
        if not claim_terms or not source_terms:
            continue
        score = len(claim_terms & source_terms) / len(claim_terms)
        if score > best_score:
            best_score, best_url = score, str(item.get("source_url", ""))

    supported = best_score >= 0.35
    return {
        "status": ToolStatus.OK.value,
        "claim": claim,
        "supported": supported,
        "best_match_url": best_url,
        "overlap_score": round(best_score, 3),
        "advice": (
            f"Cite {best_url} inline for this claim."
            if supported
            else "No gathered evidence supports this sentence. Either remove it, soften "
            "it to an explicitly-flagged opinion, or gather evidence for it first."
        ),
    }
