"""Guided error handling for tools.

The principle
-------------
A tool that raises kills the turn. A tool that returns ``{"error": "failed"}``
is barely better: the model has no idea what to do next, so it either retries
the identical call or fabricates a result.

Every ContentForge tool instead returns a **guided error envelope** - a
structured object that tells the model three things:

1. ``error_code`` - a stable, machine-readable category it can branch on.
2. ``message`` - what went wrong, in plain language.
3. ``recovery`` - *the specific next action to take*, phrased as an instruction.
   This is the field that turns a dead end into a productive next step.
4. ``retryable`` - whether re-calling with corrected arguments can succeed at
   all, which stops the model burning turns retrying a permanent failure.

The same envelope shape is produced by:

* :func:`validated_call` - for schema/argument errors, where the recovery text
  is generated from the Pydantic validation failure itself; and
* the ``on_tool_error_callback`` in
  :mod:`content_forge.plugins.observability_plugin` - the backstop that catches
  any unexpected exception escaping a tool and converts it into the same shape,
  so an unhandled bug degrades into guidance rather than a crash.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)


class ErrorCode(str, Enum):
    """Stable error categories the model can branch on."""

    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    POLICY_BLOCKED = "policy_blocked"
    CONFIRMATION_REQUIRED = "confirmation_required"
    INTERNAL = "internal"


#: Default recovery guidance per code. Individual call sites override with
#: something more specific whenever they can; these are the sane fallbacks.
_DEFAULT_RECOVERY: dict[ErrorCode, str] = {
    ErrorCode.INVALID_ARGUMENTS: (
        "Re-read this tool's parameter schema, correct the arguments listed in "
        "'field_errors', and call the tool again. Do not invent values for fields "
        "you do not know - ask the user instead."
    ),
    ErrorCode.NOT_FOUND: (
        "No record matched. Broaden or rephrase your query and retry once. If it "
        "still returns nothing, tell the user the information is unavailable "
        "rather than guessing at an answer."
    ),
    ErrorCode.UPSTREAM_UNAVAILABLE: (
        "A dependency is temporarily unreachable. Continue with the information you "
        "already have, explicitly note the gap in your output, and do not block the "
        "pipeline on this call."
    ),
    ErrorCode.PERMISSION_DENIED: (
        "This operation is not permitted with the current credentials. Do not retry. "
        "Report the missing permission to the user and stop."
    ),
    ErrorCode.RATE_LIMITED: (
        "Quota exhausted. Do not retry immediately. Summarise progress so far and "
        "tell the user to resume shortly."
    ),
    ErrorCode.POLICY_BLOCKED: (
        "A safety or brand policy blocked this action. Do not attempt to rephrase "
        "the request to get around the block. Explain the constraint to the user."
    ),
    ErrorCode.CONFIRMATION_REQUIRED: (
        "This action needs explicit human approval before it can run. Present the "
        "exact action and its consequences to the user and wait for their decision."
    ),
    ErrorCode.INTERNAL: (
        "An unexpected internal error occurred. Do not retry the identical call. "
        "Report the failure to the user with the correlation id so it can be traced."
    ),
}


def tool_error(
    code: ErrorCode,
    message: str,
    *,
    recovery: str | None = None,
    retryable: bool | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a guided error envelope for return from a tool.

    Args:
        code: Stable machine-readable category from :class:`ErrorCode`.
        message: Human-readable description of what went wrong. Must not contain
            secrets, credentials, or raw PII - this string reaches the model and
            the logs.
        recovery: Explicit instruction for the model's next action. Defaults to
            the standard guidance for ``code``.
        retryable: Whether a corrected retry can succeed. Defaults to True for
            argument/transient errors and False for permanent ones.
        **extra: Additional structured context merged into the envelope, e.g.
            ``field_errors`` or ``available_options``.

    Returns:
        A JSON-serialisable dict with ``status='error'`` plus the fields above.
    """
    if retryable is None:
        retryable = code in {
            ErrorCode.INVALID_ARGUMENTS,
            ErrorCode.NOT_FOUND,
            ErrorCode.UPSTREAM_UNAVAILABLE,
            ErrorCode.RATE_LIMITED,
        }
    envelope: dict[str, Any] = {
        "status": "error",
        "error_code": code.value,
        "message": message,
        "recovery": recovery or _DEFAULT_RECOVERY[code],
        "retryable": retryable,
    }
    envelope.update(extra)
    return envelope


def _format_field_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Flatten a Pydantic error into per-field guidance the model can act on."""
    formatted: list[dict[str, str]] = []
    for err in exc.errors():
        location = ".".join(str(p) for p in err["loc"]) or "<root>"
        formatted.append(
            {
                "field": location,
                "problem": err["msg"],
                "received_type": err.get("type", "unknown"),
            }
        )
    return formatted


def validate_arguments(
    schema: type[TModel], tool_name: str, **kwargs: Any
) -> tuple[TModel | None, dict[str, Any] | None]:
    """Validate raw tool arguments against ``schema``.

    Called as the first statement of every tool body. This is deliberately a
    helper rather than a decorator: ADK derives the function declaration the
    model sees from the tool's *real* signature via :func:`inspect.signature`,
    so wrapping tools in a ``**kwargs`` decorator would erase their parameter
    names and types. Keeping signatures flat and explicit means the model gets
    an accurate schema, and validation still happens in exactly one place.

    Args:
        schema: The Pydantic model describing this tool's arguments.
        tool_name: Name of the calling tool, used in the error message.
        **kwargs: The raw arguments as received from the model.

    Returns:
        A ``(model, None)`` pair on success, or ``(None, error_envelope)`` on
        failure. The error envelope names each offending field, states the
        problem, and embeds the full expected JSON Schema so the model can
        self-correct on its next call.

    Example:
        >>> request, error = validate_arguments(SeoScoreRequest, "score_draft", **kwargs)
        >>> if error:
        ...     return error
    """
    try:
        return schema(**kwargs), None
    except ValidationError as exc:
        field_errors = _format_field_errors(exc)
        logger.warning(
            "tool_argument_validation_failed",
            extra={"tool": tool_name, "field_errors": field_errors},
        )
        return None, tool_error(
            ErrorCode.INVALID_ARGUMENTS,
            f"{tool_name} received arguments that do not match its schema.",
            field_errors=field_errors,
            expected_schema=schema.model_json_schema(),
        )
