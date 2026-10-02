# syntax=docker/dockerfile:1.7
#
# Nour server brain image (docs/SPEC.md §4 "the brain runs on a server 24/7";
# §16 reference stack: containers in a UAE region, a separate container for the
# auditor on a different model). One image, two roles: the brain (`nour serve`)
# and the auditor (`nour auditor run --daily`, see docker-compose.yml).
#
# Security properties of this image (SPEC §13 baseline controls):
#   - no secrets baked in: nothing under .env, secrets/ or vault_data/ is copied,
#     and no ARG/ENV carries a credential; tokens come from the secrets manager
#     or the runtime environment (§13 "no secrets in prompts/repos")
#   - runs as an unprivileged user with no shell, no home-directory writes
#   - slim runtime without uv, compilers or the dev dependency group
#   - deterministic: uv.lock is enforced with --locked

ARG PYTHON_VERSION=3.11
ARG UV_VERSION=0.8.17

# ---------------------------------------------------------------------------
# uv binary (copied, never installed via curl | sh)
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------------------
# builder: resolve and install the locked dependency set into /app/.venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Layer 1: third-party dependencies only (cached until pyproject/uv.lock change).
# `postgres` extra pulls psycopg[binary], which bundles libpq: no apt packages needed.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project --extra postgres

# Layer 2: the package itself (non-editable, so the venv is self-contained).
COPY pyproject.toml uv.lock README.md ./
COPY nour ./nour
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra postgres

# ---------------------------------------------------------------------------
# runtime: slim image, non-root, only the venv + config + prompts
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

# ca-certificates for TLS to model vendors / WhatsApp / bank APIs; tzdata so
# Asia/Dubai resolves (SPEC §14: Dubai time is the system clock).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" nour \
    && useradd --uid "${APP_UID}" --gid nour --no-create-home \
         --home-dir /app --shell /usr/sbin/nologin nour \
    && mkdir -p /app /data/vault /data/audit-reports \
    && chown -R nour:nour /app /data

WORKDIR /app

COPY --from=builder --chown=nour:nour /app/.venv /app/.venv
# Version-controlled, owner-editable, non-secret config (SPEC §15 "Config files")
# and prompts (§18). docker-compose.yml bind-mounts the live copies read-only on top.
COPY --chown=nour:nour config ./config
COPY --chown=nour:nour prompts ./prompts

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Dubai \
    NOUR_TIMEZONE=Asia/Dubai \
    NOUR_CONFIG_DIR=/app/config \
    NOUR_PROMPTS_DIR=/app/prompts \
    NOUR_ENV=production

LABEL org.opencontainers.image.title="nour" \
      org.opencontainers.image.description="Nour server brain and auditor (docs/SPEC.md)" \
      org.opencontainers.image.licenses="Proprietary"

USER nour

# The FastAPI/uvicorn event bus ingress (webhooks for WhatsApp, mail, timers).
EXPOSE 8000

# Liveness through the CLI, so the check also fails if config fails to load.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["nour", "health"]

# No ENTRYPOINT on purpose: compose overrides the whole command for the auditor.
CMD ["nour", "serve"]
