"""FastAPI application entrypoint.

Bootstrap order matters:
    1. Configure structlog FIRST (so all later logs are structured JSON).
    2. Initialize observability (fail-safe) — Azure Monitor + OTEL.
    3. Install the error contract (app/errors.py): exception handlers + the
       catch-all UnhandledErrorMiddleware, the /v1 request-size cap
       (app/body_limit.py) around it, then TraceMiddleware OUTSIDE both so a
       500 or a 413 still carries the request's trace id (x-trace-id lifecycle).
    4. Include routers: operational (/ping, /health, /info) unversioned, plus the
       mandatory /v1 business router (ADR-0001).
    5. On startup, emit the degraded-mode WARNING if observability is disabled.
    6. On shutdown, dispose the (lazy) SQLAlchemy engine so pooled DB connections
       are closed cleanly (ADR-0004).

Run with working dir = apps/api:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
Local dev env: `apps/api/.env` (copy of `.env.example`) is loaded by
``load_local_env()`` below IF IT EXISTS — it is optional, and ambient/shell env
always wins. Docker/Compose pass real env vars instead; no `--env-file` flag is
needed (uvicorn's `--env-file` hard-fails when the file is absent).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.ai.config import (
    ASK_WORST_CASE_LIMIT_SECONDS,
    INGRESS_REQUEST_TIMEOUT_SECONDS,
    get_ai_settings,
)
from app.body_limit import BodyLimitMiddleware
from app.config import get_settings, load_local_env
from app.db import dispose_engine
from app.errors import UnhandledErrorMiddleware, install_error_handlers
from app.events import track_event
from app.logging_config import configure_logging, get_logger
from app.observability import get_observability_state, init_observability
from app.routers import v1_router
from app.routes import router as api_router
from app.tracing import TraceMiddleware

# 0. Optional local-dev env file (no-op when absent, never overrides ambient env).
load_local_env()

# 1. Structured logging must be configured before anything else logs.
configure_logging()
_log = get_logger("app.main")


def _warn_if_the_ask_deadline_is_too_long() -> None:
    """One answer's worst case must fit the ingress request timeout (Azure Container Apps: 240 s,
    not configurable), or a slow answer is cut by the ingress instead of ending in an error frame.
    Numbers only in the log. Never raises: a bad AI setting is reported where it is used."""
    try:
        ai = get_ai_settings()
    except Exception:  # noqa: BLE001 — startup must not fail on an AI setting
        return
    if not ai.fits_the_ingress_limit:
        _log.warning(
            "the ask deadline is longer than the ingress request timeout",
            worst_case_seconds=ai.worst_case_seconds,
            limit_seconds=ASK_WORST_CASE_LIMIT_SECONDS,
            ingress_seconds=INGRESS_REQUEST_TIMEOUT_SECONDS,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook: surface degraded mode loudly at boot."""
    settings = get_settings()
    state = get_observability_state()
    _log.info(
        "service starting",
        otel_service_name=settings.otel_service_name,
        port=settings.port,
        observability_enabled=state["enabled"],
    )
    if not state["enabled"]:
        _log.warning(
            "Observability disabled: no Azure connection string "
            "— running with local stdout logging only",
            reason=state["reason"],
        )
    _warn_if_the_ask_deadline_is_too_long()
    # First wired custom event (Phase 3): degraded mode → local line only.
    track_event("service.start")
    yield
    # Close pooled DB connections (no-op if the lazy engine was never created).
    await dispose_engine()
    _log.info("service stopping")


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="Team Assistant — api",
        version="0.0.0",
        lifespan=lifespan,
    )

    # 2. Fail-safe observability (must run before instrumentation-aware middleware).
    init_observability(app)

    # 3. Error contract: handlers + catch-all, with the trace middleware OUTERMOST
    #    (Starlette wraps in reverse order of add_middleware calls).
    install_error_handlers(app)
    app.add_middleware(UnhandledErrorMiddleware)
    # The request-size cap sits between them: a refusal still carries the trace id, and an
    # oversized body is refused before anything downstream buffers it (api 5.5).
    app.add_middleware(BodyLimitMiddleware, max_bytes=get_settings().max_request_body_bytes)
    app.add_middleware(TraceMiddleware)

    # 4. Routes: operational (unversioned) + the mandatory /v1 business surface.
    app.include_router(api_router)
    app.include_router(v1_router)

    return app


# Module-level app so `uvicorn app.main:app` works.
app = create_app()
