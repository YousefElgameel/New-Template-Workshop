# syntax=docker/dockerfile:1
# =============================================================================
# team-assistant-api — multi-stage build. BUILD CONTEXT = THE REPOSITORY ROOT:
# the image carries the repo's docs/**/*.md as its corpus (/app/corpus, ADR-0013),
# and docs/ is not inside apps/api. Build it from the repo root:
#   docker build --file apps/api/Dockerfile --tag team-assistant-api .
# (`docker compose build api` does the same; the root .dockerignore applies.)
# uv is installed via pip (pinned) so the build does not depend on an external
# container registry. Runtime carries only the synced venv + app code + corpus.
# =============================================================================

# --- Stage 1: build the venv from the lockfile ------------------------------
FROM python:3.14-slim AS builder

# Install uv (pinned) without relying on an external image registry.
RUN pip install --no-cache-dir --only-binary=:all: uv==0.10.9

# Production-friendly uv behavior.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 1) Install dependencies only (no project) for maximal layer caching.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=apps/api/uv.lock,target=uv.lock \
    --mount=type=bind,source=apps/api/pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --no-build

# 2) Add the project source and install the project itself.
# NOTE: `--no-build` is intentionally omitted here — this step builds only our
# own first-party package (declarative hatchling, no setup.py). All third-party
# deps were already installed wheel-only with `--no-build` in step 1, so no
# untrusted setup scripts execute here. (`--no-build` would error: the local
# editable project "has no binary distribution".)
COPY apps/api /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Stage 2: the corpus, checked for secrets before it can be copied --------
# Only the .md files of docs/ (no hidden file or directory, no symlink) are kept,
# laid out as the ingest job reads them: /corpus/<path inside docs>, whose
# `source` becomes docs/<path>. A secret-looking string in any of them fails the
# build here: the check prints the file, line and kind of pattern, never the text.
FROM python:3.14-slim AS corpus
WORKDIR /work
COPY apps/api/scripts/check_corpus_secrets.py apps/api/scripts/corpus-secrets-allowlist.txt /work/
COPY docs /work/docs
RUN mkdir /corpus \
 && cd /work/docs \
 && find . -type f -name '*.md' -not -path '*/.*' -exec cp --parents -t /corpus {} + \
 && test -n "$(ls -A /corpus)" \
 && python /work/check_corpus_secrets.py /corpus

# --- Stage 3: lean runtime image --------------------------------------------
FROM python:3.14-slim AS runtime

# Use the project's venv directly (no uv needed at runtime).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    APP_ENV=prod \
    PORT=8000

# Microsoft ODBC Driver 18 for SQL Server + unixODBC — required by pyodbc for
# the `mssql+aioodbc` engine (ADR-0008). Follows Microsoft's Debian install
# steps (the base image is Debian; VERSION_ID selects the matching repo config).
# ACCEPT_EULA acknowledges the driver's licence. curl stays installed: the
# driver's Entra ID (managed identity) auth path uses libcurl at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg2 \
 && DEBIAN_VERSION="$(grep VERSION_ID /etc/os-release | cut -d '"' -f 2 | cut -d '.' -f 1)" \
 && curl -sSL -o /tmp/packages-microsoft-prod.deb \
      "https://packages.microsoft.com/config/debian/${DEBIAN_VERSION}/packages-microsoft-prod.deb" \
 && dpkg -i /tmp/packages-microsoft-prod.deb \
 && rm /tmp/packages-microsoft-prod.deb \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc libgssapi-krb5-2 \
 && apt-get purge -y --auto-remove gnupg2 \
 && rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN groupadd --system app && useradd --system --gid app --create-home appuser

WORKDIR /app

# Copy the synced venv and the application code.
COPY --from=builder --chown=appuser:app /app/.venv /app/.venv
COPY --chown=appuser:app apps/api/app /app/app
# Alembic toolchain ships in the image so a HUMAN can run migrations from it
# (e.g. `docker run --rm -e DATABASE_URL=... team-assistant-api alembic upgrade head`).
# The service itself never runs migrations; CMD is unchanged.
COPY --chown=appuser:app apps/api/alembic.ini /app/alembic.ini
COPY --chown=appuser:app apps/api/alembic /app/alembic
# The corpus the ingest job reads when APP_ENV is not `local` (app/ai/ingest.py,
# IMAGE_CORPUS_DIR). Root-owned and world-readable on purpose: the service reads it and
# cannot rewrite what the next ingest run would embed.
COPY --from=corpus /corpus /app/corpus

USER appuser

EXPOSE 8000

# Liveness probe (satisfies Checkov CKV_DOCKER_2). python:3.14-slim ships no
# wget/curl, so probe /health with the Python stdlib.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
