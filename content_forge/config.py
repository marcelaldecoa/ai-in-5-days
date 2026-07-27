"""Centralised, validated configuration for ContentForge.

Design rules enforced here (see README "Secure secret management"):

1. **No secret literal ever lives in code, in `.env.example`, or in a container
   image.** Settings only ever carry the *resource name* of a secret
   (``projects/*/secrets/*/versions/*``). The value itself is fetched on demand
   from Google Secret Manager via :func:`resolve_secret`.
2. **Fetches are cached** for the process lifetime so a hot tool call never pays
   a network round trip, and are lazily imported so that the core package can be
   installed and unit-tested without the GCP client libraries present.
3. **Failure is explicit.** A missing secret raises :class:`SecretResolutionError`
   with an actionable message rather than silently yielding ``None`` and turning
   into a confusing 401 three call-frames later.
"""

from __future__ import annotations

import functools
import logging
import os
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from dotenv import find_dotenv, load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _load_dotenv_into_environment() -> None:
    """Load `.env` into ``os.environ`` at import time.

    This is load-bearing, not a convenience. Two different consumers read
    configuration, and only one of them understands `.env`:

    * :class:`Settings` below is a pydantic-settings model, so it reads `.env`
      itself - but **only for its own ``CONTENTFORGE_``-prefixed fields**.
    * ``GOOGLE_API_KEY`` and ``GOOGLE_GENAI_USE_VERTEXAI`` are read straight
      from ``os.environ`` by the ``google-genai`` SDK, and by
      :meth:`Settings.credential_mode` here.

    pydantic-settings does not export what it reads back into ``os.environ``.
    Without this call, putting ``GOOGLE_API_KEY`` in `.env` - exactly what the
    quickstart instructs - leaves the SDK seeing nothing, and the agent fails
    as though no key had been provided at all.

    ``override=False`` is deliberate: a variable already exported in the shell,
    or injected by Cloud Run, must win over a stale `.env` left in a working
    copy. `.env` is a local convenience, never an authority.
    """
    try:
        # usecwd=True so it resolves relative to where the command was run,
        # not to this file's location inside an installed package.
        path = find_dotenv(usecwd=True)
        if path:
            load_dotenv(path, override=False)
    except Exception:  # noqa: BLE001 - a malformed .env must not stop the process
        logger.warning("dotenv_load_failed", exc_info=True)


_load_dotenv_into_environment()


class SecretResolutionError(RuntimeError):
    """Raised when a configured secret reference cannot be resolved.

    Carries the secret's *resource name* (never its value) plus the remediation
    step, so the message is safe to log and useful to an on-call engineer.
    """


class Settings(BaseSettings):
    """Runtime configuration, populated from environment and `.env`.

    Every field is validated at import time, so a misconfigured deployment fails
    fast at container start rather than mid-conversation.
    """

    model_config = SettingsConfigDict(
        env_prefix="CONTENTFORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Google Cloud -------------------------------------------------------
    # These two use the Google-standard names (no CONTENTFORGE_ prefix) because
    # the ADK and google-genai SDKs read them directly.
    project_id: str = Field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    location: str = Field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    environment: Literal["local", "dev", "staging", "prod"] = "local"

    # --- Secret *references* (never values) ---------------------------------
    gemini_api_key_secret: str = ""
    cms_api_token_secret: str = ""
    db_password_secret: str = ""

    # --- Persistence --------------------------------------------------------
    session_backend: Literal["memory", "database", "vertex_ai"] = "database"
    database_url: str = "sqlite+aiosqlite:///./.contentforge/sessions.db"
    agent_engine_id: str = ""

    # --- Retrieval ----------------------------------------------------------
    vertex_search_datastore: str = ""

    # --- Observability ------------------------------------------------------
    log_level: str = "INFO"
    enable_cloud_trace: bool = False
    enable_dlp_redaction: bool = False
    service_name: str = "contentforge"

    # --- Context management -------------------------------------------------
    # Compact the event history every N invocations, keeping `overlap` events of
    # verbatim overlap so the summary never severs a tool-call/response pair.
    compaction_interval: int = 6
    compaction_overlap: int = 2
    # ADK requires token_threshold and event_retention_size to be set together:
    # the threshold says *when* to compact early, the retention size says how
    # many recent events stay verbatim rather than being folded into a summary.
    compaction_token_threshold: int = 24_000
    compaction_event_retention: int = 40

    # --- Context caching ----------------------------------------------------
    # Caches the stable prompt prefix (constitution + agent instructions) so it
    # is billed once per TTL rather than on every model call. The TTL is sized to
    # span a typical editorial session.
    cache_intervals: int = 10
    cache_ttl_seconds: int = 1800
    # Below this, caching overhead outweighs the saving.
    cache_min_tokens: int = 2_048

    # --- Human-in-the-loop --------------------------------------------------
    require_publish_confirmation: bool = True

    @model_validator(mode="after")
    def _validate_deployment_invariants(self) -> Settings:
        """Reject configurations that would be unsafe or non-functional.

        Catching these here means a bad deploy dies at startup with a clear
        message instead of failing on the first user turn.
        """
        if self.session_backend == "vertex_ai" and not self.agent_engine_id:
            raise ValueError(
                "session_backend='vertex_ai' requires CONTENTFORGE_AGENT_ENGINE_ID. "
                "Provision one with `terraform apply` in deployment/terraform, or "
                "switch to CONTENTFORGE_SESSION_BACKEND=database."
            )
        if self.environment == "prod":
            if not self.project_id:
                raise ValueError(
                    "GOOGLE_CLOUD_PROJECT must be set when CONTENTFORGE_ENVIRONMENT=prod."
                )
            if not self.require_publish_confirmation:
                # The human-in-the-loop gate is a safety control, not a tunable.
                raise ValueError(
                    "require_publish_confirmation cannot be disabled in prod: publishing "
                    "to the live CMS is an irreversible, externally-visible action and "
                    "must stay behind the human approval gate."
                )
        return self

    def resolved_database_url(self) -> str:
        """Return the session database URL with its password substituted in.

        The deployed URL carries a literal ``{db_password}`` placeholder rather
        than a credential, so the password is absent from the Cloud Run service's
        environment, from ``terraform show``, and from any process listing. The
        real value is fetched from Secret Manager here, at connect time.

        Returns:
            The connection URL. Returned unchanged when it contains no
            placeholder, which is the local SQLite case.

        Raises:
            SecretResolutionError: When the URL needs a password but
                ``CONTENTFORGE_DB_PASSWORD_SECRET`` is unset or unreadable.
                Failing here beats surfacing an opaque authentication error on
                the first user turn.
        """
        if "{db_password}" not in self.database_url:
            return self.database_url
        password = resolve_secret(self.db_password_secret, required=True)
        return self.database_url.replace("{db_password}", quote_plus(password or ""))

    @property
    def credential_mode(self) -> Literal["api_key", "platform_adc", "unconfigured"]:
        """How this process authenticates to Gemini.

        Two supported paths:

        * ``api_key`` - a Gemini Developer API key (AI Studio) in ``GOOGLE_API_KEY``
          or ``GEMINI_API_KEY``. Zero cloud setup; the right choice for running
          locally.
        * ``platform_adc`` - the Gemini Enterprise Agent Platform via Application
          Default Credentials or workload identity. No key exists at all, which
          is why every deployed environment uses it.

        When ``GOOGLE_GENAI_USE_VERTEXAI`` is set explicitly it always wins - and
        every deployed path (Dockerfile, Terraform, bootstrap, CI) sets it to
        ``1``. When it is *unset*, an available API key is preferred, so someone
        who exports only ``GOOGLE_API_KEY`` gets the mode they obviously meant
        rather than a confusing ADC failure.

        Returns:
            The active mode, or ``"unconfigured"`` when neither is available.
        """
        explicit = os.getenv("GOOGLE_GENAI_USE_VERTEXAI")
        has_key = bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))

        if explicit is not None:
            if explicit.strip().lower() in ("0", "false", "no", ""):
                return "api_key" if has_key else "unconfigured"
            return "platform_adc"

        if has_key:
            return "api_key"
        # No key and no explicit choice: ADC may still be present (gcloud login,
        # or a service account on a Cloud Run instance). Reporting it as
        # unconfigured would be wrong, but so would claiming it works - the
        # doctor probes for real credentials rather than guessing here.
        return "platform_adc" if self.project_id else "unconfigured"

    @property
    def uses_vertex_ai(self) -> bool:
        """True when models are served through the platform rather than an API key."""
        return self.credential_mode == "platform_adc"

    @property
    def local_state_dir(self) -> Path:
        """Directory for local-mode artefacts (SQLite session db, eval output)."""
        path = Path(".contentforge")
        path.mkdir(parents=True, exist_ok=True)
        return path


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton."""
    return Settings()


@functools.lru_cache(maxsize=32)
def resolve_secret(secret_resource_name: str, *, required: bool = True) -> str | None:
    """Fetch a secret's value from Google Secret Manager.

    This is the *only* path by which a credential enters the process. Tools and
    clients call it at use time; they never accept a raw credential argument and
    never read one from a plain environment variable.

    Args:
        secret_resource_name: Fully-qualified Secret Manager version name, e.g.
            ``projects/123456/secrets/cms-api-token/versions/latest``. An empty
            string means "not configured".
        required: When True, an unresolvable secret raises. When False, the
            function returns ``None`` so the caller can degrade to a mock or
            read-only mode (used by the local/offline developer experience).

    Returns:
        The secret payload as a UTF-8 string, or ``None`` when the reference is
        unset/unresolvable and ``required`` is False.

    Raises:
        SecretResolutionError: When the secret cannot be resolved and it is
            required. The message names the secret resource and the fix; it
            never contains the secret value.
    """
    if not secret_resource_name:
        if required:
            raise SecretResolutionError(
                "A required secret reference is empty. Set the corresponding "
                "CONTENTFORGE_*_SECRET environment variable to a Secret Manager "
                "resource name (projects/*/secrets/*/versions/latest). See .env.example."
            )
        return None

    try:
        # Imported lazily: the core package must remain installable and testable
        # without the GCP client libraries (see pyproject `gcp` extra).
        from google.cloud import secretmanager
    except ImportError as exc:
        if required:
            raise SecretResolutionError(
                f"Cannot read secret {secret_resource_name!r}: google-cloud-secret-manager "
                "is not installed. Install it with `pip install -e '.[gcp]'`."
            ) from exc
        logger.warning(
            "secret_manager_unavailable",
            extra={"secret_resource": secret_resource_name, "degraded_to": "unset"},
        )
        return None

    try:
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=secret_resource_name)
        return response.payload.data.decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed, actionable error
        if required:
            raise SecretResolutionError(
                f"Failed to access secret {secret_resource_name!r}. Verify that the "
                "secret exists and that the runtime service account holds "
                "roles/secretmanager.secretAccessor on it (granted in "
                "deployment/terraform/secrets.tf)."
            ) from exc
        logger.warning(
            "secret_access_failed",
            extra={"secret_resource": secret_resource_name, "degraded_to": "unset"},
        )
        return None
