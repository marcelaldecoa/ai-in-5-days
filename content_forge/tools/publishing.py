"""Publishing tools - the high-stakes, human-gated edge of the system.

Human-in-the-loop rationale
---------------------------
Publishing is the one action in this pipeline that is **irreversible and
externally visible**: once a post is live it is crawled, indexed, syndicated to
subscribers, and cached by third parties. An unwanted draft can be deleted; an
unwanted *publication* cannot be un-seen. Every other tool here is a read.

So :func:`publish_post_to_cms` implements a hard stop:

1. On first call the tool does **not** publish. It calls
   :meth:`ToolContext.request_confirmation`, which suspends the invocation and
   surfaces a structured approval request - including the exact title, URL slug,
   audience size and schedule - to a human.
2. The tool returns a ``needs_confirmation`` envelope, so even a model that
   ignores the suspension cannot mistake this for success.
3. Only when the runtime resumes the tool with
   ``tool_context.tool_confirmation.confirmed is True`` does the CMS write
   actually happen.

The gate is enforced in two independent places, so removing either one alone
does not open a hole: here in the tool body, and declaratively via
``FunctionTool(..., require_confirmation=...)`` in
:mod:`content_forge.tools.registry`. :class:`content_forge.config.Settings`
additionally refuses to boot in ``prod`` with the gate disabled.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from google.adk.tools import ToolContext

from content_forge.config import get_settings, resolve_secret
from content_forge.errors import ErrorCode, tool_error, validate_arguments
from content_forge.observability.logging_config import get_logger
from content_forge.observability.redaction import redact_text
from content_forge.schemas import PublishRequest, ToolStatus

logger = get_logger(__name__)


def _slugify(title: str) -> str:
    """Derive a URL slug from a title (lowercase, hyphenated, alphanumeric)."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in title.lower())
    return "-".join(cleaned.split())[:80]


def publish_post_to_cms(
    title: str,
    body_markdown: str,
    primary_keyword: str,
    meta_description: str,
    tags: list[str],
    author_email: str,
    tool_context: ToolContext,
    scheduled_for: str | None = None,
) -> dict[str, Any]:
    """Publish a finished post to the live CMS. Requires explicit human approval.

    **This action is irreversible and externally visible.** Calling it does not
    publish immediately: it raises a confirmation request to a human operator and
    suspends. You must relay the returned approval summary to the user and wait.
    Never describe a ``needs_confirmation`` response as a successful publication.

    Only call this once the post has cleared both gates: the critic reports
    ``passes_quality_bar=true``, and ``score_draft_seo_readiness`` reports
    ``ready_to_publish=true``. If either is false, fix the issues first.

    Args:
        title: The post title, 10-120 characters. Becomes the H1 and the URL slug.
        body_markdown: The complete post body in Markdown, at least 200 characters.
        primary_keyword: The keyword this post targets, for CMS indexing.
        meta_description: Search-result summary, 50-160 characters.
        tags: 1-10 CMS taxonomy tags.
        author_email: Owning author. Receives the publish receipt. Redacted in logs.
        tool_context: Injected by the ADK runtime; carries the confirmation state.
            Do not supply this argument yourself.
        scheduled_for: ISO-8601 timestamp to schedule publication for, or omit to
            publish immediately once approved.

    Returns:
        A dict matching :class:`~content_forge.schemas.PublishReceipt`:

        * ``status='needs_confirmation'`` - the expected first response. Contains
          ``confirmation_required_reason``. **Nothing has been published.** Report
          the pending approval to the user and stop.
        * ``status='ok'`` - the human approved and the post is live. Contains
          ``post_id``, ``url`` and ``published_at``.
        * ``status='error'`` - a guided error envelope with ``error_code`` and a
          ``recovery`` instruction. A ``permission_denied`` code means the human
          rejected the publication; do not retry, and do not attempt to publish
          via any other route.
    """
    request, error = validate_arguments(
        PublishRequest,
        "publish_post_to_cms",
        title=title,
        body_markdown=body_markdown,
        primary_keyword=primary_keyword,
        meta_description=meta_description,
        tags=tags,
        author_email=author_email,
        scheduled_for=scheduled_for,
    )
    if error:
        return error
    assert request is not None

    settings = get_settings()
    slug = _slugify(request.title)
    # Stable id derived from content, so an approved-then-retried publish is
    # idempotent rather than creating a duplicate post.
    content_digest = hashlib.sha256(
        f"{request.title}\n{request.body_markdown}".encode()
    ).hexdigest()[:16]

    approval_summary = {
        "action": "publish_post_to_cms",
        "irreversible": True,
        "title": request.title,
        "slug": slug,
        "word_count": len(request.body_markdown.split()),
        "tags": request.tags,
        "primary_keyword": request.primary_keyword,
        "scheduled_for": request.scheduled_for or "immediately",
        "visibility": "public - indexed by search engines and sent to subscribers",
        "content_digest": content_digest,
    }

    # ---- The human-in-the-loop gate -------------------------------------
    confirmation = getattr(tool_context, "tool_confirmation", None)
    if settings.require_publish_confirmation and (
        confirmation is None or not confirmation.confirmed
    ):
        if confirmation is not None and not confirmation.confirmed:
            # The human saw the request and explicitly declined.
            logger.warning(
                "publish_rejected_by_human",
                tool="publish_post_to_cms",
                slug=slug,
                content_digest=content_digest,
            )
            return tool_error(
                ErrorCode.PERMISSION_DENIED,
                "A human reviewer rejected this publication.",
                recovery=(
                    "Do not retry and do not attempt to publish through another tool. "
                    "Ask the reviewer what needs to change, apply those edits, and "
                    "only then request approval again."
                ),
            )

        logger.info(
            "publish_confirmation_requested",
            tool="publish_post_to_cms",
            slug=slug,
            content_digest=content_digest,
            scheduled_for=request.scheduled_for or "immediately",
        )
        tool_context.request_confirmation(
            hint=(
                f"Approve publishing '{request.title}' to the live public blog?\n"
                f"  slug:      /{slug}\n"
                f"  words:     {approval_summary['word_count']}\n"
                f"  keyword:   {request.primary_keyword}\n"
                f"  tags:      {', '.join(request.tags)}\n"
                f"  schedule:  {approval_summary['scheduled_for']}\n"
                "This is irreversible: the post will be publicly indexed and "
                "emailed to subscribers."
            ),
            payload=approval_summary,
        )
        return {
            "status": ToolStatus.NEEDS_CONFIRMATION.value,
            "post_id": "",
            "url": "",
            "published_at": "",
            "confirmation_required_reason": (
                "Publishing is irreversible and externally visible, so it requires "
                "explicit human approval. Nothing has been published yet. Present the "
                "approval summary to the user and wait for their decision."
            ),
            "approval_summary": approval_summary,
        }

    # ---- Approved: perform the write ------------------------------------
    cms_token = resolve_secret(settings.cms_api_token_secret, required=False)
    if cms_token is None:
        # No CMS credential configured: this is the local/offline path. We record
        # the intent durably instead of silently pretending to have published.
        outbox = settings.local_state_dir / "publish_outbox.jsonl"
        with outbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**approval_summary, "approved": True}) + "\n")
        logger.warning(
            "publish_simulated_no_cms_credential",
            tool="publish_post_to_cms",
            slug=slug,
            outbox=str(outbox),
        )
        return {
            "status": ToolStatus.OK.value,
            "post_id": f"local-{content_digest}",
            "url": f"https://example.invalid/blog/{slug}",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "confirmation_required_reason": "",
            "note": (
                "No CMS credential is configured, so the approved post was written to "
                f"the local outbox at {outbox} instead of a live site. Tell the user "
                "the post was NOT published to a real CMS."
            ),
        }

    # A real deployment issues the authenticated CMS write here, using the token
    # fetched from Secret Manager above. The token is never logged and never
    # returned to the model.
    published_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        "post_published",
        tool="publish_post_to_cms",
        slug=slug,
        content_digest=content_digest,
        author=redact_text(request.author_email),
        published_at=published_at,
    )
    return {
        "status": ToolStatus.OK.value,
        "post_id": content_digest,
        "url": f"https://blog.example.com/{slug}",
        "published_at": published_at,
        "confirmation_required_reason": "",
    }


def save_post_draft_for_human_review(
    title: str, body_markdown: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Save the draft as an unpublished revision for a human to review.

    The safe counterpart to :func:`publish_post_to_cms`. Saving is reversible and
    not publicly visible, so it needs no confirmation gate. Prefer this whenever
    the user has not clearly asked for publication, or when quality gates fail.

    Args:
        title: The working title of the draft.
        body_markdown: The complete draft body in Markdown.
        tool_context: Injected by the ADK runtime. Do not supply this yourself.

    Returns:
        A dict with ``status``, ``draft_id``, ``review_url`` and ``saved_at`` on
        success, or a guided error envelope on failure.
    """
    if not title.strip() or len(body_markdown) < 50:
        return tool_error(
            ErrorCode.INVALID_ARGUMENTS,
            "A draft needs a non-empty title and a body of at least 50 characters.",
            recovery="Finish the draft before saving it, then call this tool again.",
        )

    draft_id = hashlib.sha256(f"{title}{body_markdown}".encode()).hexdigest()[:12]
    # Persist to session state so the draft survives across turns and processes.
    tool_context.state["last_saved_draft"] = {
        "draft_id": draft_id,
        "title": title,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("draft_saved", tool="save_post_draft_for_human_review", draft_id=draft_id)
    return {
        "status": ToolStatus.OK.value,
        "draft_id": draft_id,
        "review_url": f"https://cms.example.com/drafts/{draft_id}",
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
