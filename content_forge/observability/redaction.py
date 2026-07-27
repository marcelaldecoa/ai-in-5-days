"""PII redaction applied before anything is logged, traced, or memorised.

Threat model
------------
An editorial agent handles user-pasted briefs, interview transcripts and source
documents. Those routinely contain personal data that has no business being
durably stored. Three sinks in this system are durable and therefore dangerous:

1. **Structured logs** -> Cloud Logging, retained and widely readable.
2. **Trace span attributes** -> Cloud Trace, same exposure.
3. **Long-term memory** -> persisted verbatim and *re-injected into future
   prompts*, which turns one leak into a permanent one.

:func:`redact_text` is wired into all three: the structlog processor chain
(:mod:`content_forge.observability.logging_config`), the span attribute setter
(:mod:`content_forge.observability.tracing`), and the async memory writer
(:mod:`content_forge.plugins.memory_plugin`).

Two-tier strategy
-----------------
* **Always on:** deterministic regex scrubbing. No network call, no dependency,
  no failure mode - so redaction cannot be "temporarily unavailable".
* **Optionally on:** Google Cloud DLP for the classes regex cannot catch
  (person names, addresses, demographic data). Enabled with
  ``CONTENTFORGE_ENABLE_DLP_REDACTION=1``. DLP runs *in addition to*, never
  instead of, the regex tier - so a DLP outage degrades coverage rather than
  disabling redaction entirely.
"""

from __future__ import annotations

import functools
import logging
import re
from typing import Any

from content_forge.config import get_settings

logger = logging.getLogger(__name__)

#: Ordered (name, pattern, replacement) rules. Order matters: more specific
#: patterns run first so a credit card is not partially eaten by the phone rule.
_REDACTION_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "credential",
        # API keys / bearer tokens / private key blocks. Highest priority: a
        # leaked credential is worse than a leaked phone number.
        re.compile(
            r"(?i)\b(?:sk-|pk-|ghp_|gho_|AIza|ya29\.)[A-Za-z0-9._\-]{10,}\b"
            r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
            r"|(?:(?:bearer|token|api[_-]?key|password|secret)\s*[:=]\s*)[\"']?[A-Za-z0-9._\-]{8,}"
        ),
        "[REDACTED_CREDENTIAL]",
    ),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    (
        "credit_card",
        re.compile(r"\b(?:\d[ \-]*?){13,19}\b"),
        "[REDACTED_CARD]",
    ),
    (
        "us_ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[REDACTED_SSN]",
    ),
    (
        "phone",
        re.compile(
            r"(?:\+\d{1,3}[ \-.]?)?(?:\(\d{2,4}\)[ \-.]?)?\b\d{3}[ \-.]\d{3,4}[ \-.]\d{4}\b"
        ),
        "[REDACTED_PHONE]",
    ),
    (
        "ip_address",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        "[REDACTED_IP]",
    ),
    (
        "iban",
        re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
        "[REDACTED_IBAN]",
    ),
]

#: DLP infoTypes used when cloud redaction is enabled. These complement the
#: regex tier with classes regex fundamentally cannot detect.
_DLP_INFO_TYPES: list[str] = [
    "PERSON_NAME",
    "STREET_ADDRESS",
    "DATE_OF_BIRTH",
    "PASSPORT",
    "US_DRIVERS_LICENSE_NUMBER",
    "CREDIT_CARD_NUMBER",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
]

#: Keys whose values are always dropped wholesale, regardless of content.
_ALWAYS_DROP_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "token",
        "cookie",
        "set-cookie",
        "x-api-key",
        "credentials",
        "private_key",
    }
)


def redact_text(value: str) -> str:
    """Scrub PII and credentials from a string.

    Safe to call on any value bound for a log, span or memory record. Always
    applies the deterministic regex tier; additionally applies Cloud DLP when
    ``CONTENTFORGE_ENABLE_DLP_REDACTION=1``.

    Args:
        value: The raw text to scrub.

    Returns:
        The text with detected sensitive spans replaced by ``[REDACTED_*]``
        markers. Never raises: a redaction failure must not take down the caller,
        so DLP errors fall back to the regex-only result.
    """
    if not value:
        return value

    redacted = value
    for _name, pattern, replacement in _REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)

    if get_settings().enable_dlp_redaction:
        redacted = _apply_cloud_dlp(redacted)
    return redacted


def redact_structure(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure.

    Applies :func:`redact_text` to every string, and drops values whose *key*
    is inherently sensitive (an ``authorization`` header is sensitive whatever
    its value looks like).

    Args:
        value: A dict, list, string, or scalar.
        _depth: Internal recursion guard.

    Returns:
        A redacted copy. Scalars other than strings are returned unchanged.
    """
    if _depth > 12:  # guard against pathological/cyclic structures
        return "[REDACTED_DEPTH_LIMIT]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED_SENSITIVE_KEY]"
                if str(key).lower() in _ALWAYS_DROP_KEYS
                else redact_structure(item, _depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_structure(item, _depth + 1) for item in value]
    return value


@functools.lru_cache(maxsize=1)
def _dlp_client() -> Any | None:
    """Return a cached DLP client, or None when unavailable."""
    try:
        from google.cloud import dlp_v2
    except ImportError:
        logger.warning("dlp_unavailable_install_gcp_extra")
        return None
    try:
        return dlp_v2.DlpServiceClient()
    except Exception:  # noqa: BLE001 - degrade to regex-only redaction
        logger.warning("dlp_client_init_failed")
        return None


def _apply_cloud_dlp(text: str) -> str:
    """Apply Cloud DLP de-identification on top of the regex tier.

    Returns the input unchanged (already regex-redacted) if DLP is unreachable,
    so that a DLP outage degrades coverage instead of dropping redaction.
    """
    client = _dlp_client()
    settings = get_settings()
    if client is None or not settings.project_id:
        return text

    try:
        response = client.deidentify_content(
            request={
                "parent": f"projects/{settings.project_id}/locations/global",
                "item": {"value": text},
                "inspect_config": {
                    "info_types": [{"name": name} for name in _DLP_INFO_TYPES],
                    "min_likelihood": "LIKELY",
                },
                "deidentify_config": {
                    "info_type_transformations": {
                        "transformations": [
                            {"primitive_transformation": {"replace_with_info_type_config": {}}}
                        ]
                    }
                },
            }
        )
        return response.item.value
    except Exception:  # noqa: BLE001 - never let redaction failure break the caller
        logger.warning("dlp_deidentify_failed_falling_back_to_regex")
        return text
