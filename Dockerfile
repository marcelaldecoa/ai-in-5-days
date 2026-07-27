# syntax=docker/dockerfile:1.7
###############################################################################
# ContentForge agent container.
#
# Multi-stage so the runtime image carries no build toolchain, and runs as a
# non-root user. No secret is ever baked in: credentials are resolved at runtime
# from Secret Manager using the Cloud Run service account's workload identity.
###############################################################################

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer first, so a source-only change does not invalidate it.
COPY pyproject.toml README.md ./
COPY content_forge/__init__.py content_forge/__init__.py

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[gcp]"

COPY content_forge/ content_forge/
RUN /opt/venv/bin/pip install --no-deps .


###############################################################################
# Runtime
###############################################################################
FROM python:3.12-slim AS runtime

# Non-root: a container that can publish to a public blog should not also be
# able to rewrite its own filesystem.
RUN groupadd --system --gid 1001 contentforge \
    && useradd --system --uid 1001 --gid contentforge --create-home contentforge

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Vertex AI via workload identity: no API key in the image or the env.
    GOOGLE_GENAI_USE_VERTEXAI=1 \
    CONTENTFORGE_ENABLE_CLOUD_TRACE=1 \
    CONTENTFORGE_SESSION_BACKEND=database \
    # The human-in-the-loop publish gate ships on. config.py additionally
    # refuses to boot in prod with it disabled.
    CONTENTFORGE_REQUIRE_PUBLISH_CONFIRMATION=1 \
    PORT=8080

COPY --from=builder /opt/venv /opt/venv
COPY --chown=contentforge:contentforge content_forge/ /app/content_forge/

WORKDIR /app
USER contentforge

EXPOSE 8080

# Fail fast and loudly if the agent tree cannot be constructed, rather than
# serving a broken revision.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "from content_forge.agent import app; assert app.root_agent" || exit 1

# `adk api_server` serves the agent over HTTP. `--host 0.0.0.0` is required for
# Cloud Run to route traffic to the container.
CMD ["sh", "-c", "adk api_server --host 0.0.0.0 --port ${PORT} /app"]
