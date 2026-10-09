"""Admin routes (``/v1/admin``) — roadmap api 3.3, ADR-0015.

Guarded by the shared ``x-admin-key`` (``app.admin``), **not exposed through the web BFF** (it has
explicit routes and no catch-all, so nothing forwards to ``/v1/admin``). The route takes no body.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.admin import require_admin_key
from app.ai import ingest_job
from app.ai.config import get_ai_settings
from app.errors import ErrorBody

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_key)],
    responses={
        401: {"model": ErrorBody, "description": "Missing, wrong or unset key (`unauthorized`)"}
    },
)


class IngestStarted(BaseModel):
    status: Literal["started"] = "started"


@router.post(
    "/ingest",
    status_code=202,
    response_model=IngestStarted,
    summary="Start an ingestion run in the background",
    responses={
        409: {"model": ErrorBody, "description": "A run is already in progress (`ingest_running`)"},
        503: {
            "model": ErrorBody,
            "description": "Search or embeddings not configured (`ai_unavailable`)",
        },
    },
)
async def start_ingest() -> IngestStarted:
    """Ingest the corpus; the outcome is in the log and the `ingest.completed` event."""
    # A deployment that cannot ingest says so now, not in a log line minutes later.
    settings = get_ai_settings()
    settings.require_search()
    settings.require_embeddings()
    ingest_job.start()
    return IngestStarted()
