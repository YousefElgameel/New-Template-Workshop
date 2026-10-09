"""The ingestion run behind ``POST /v1/admin/ingest`` (api roadmap 3.3; decision 19).

One run at a time, **in this process** — a second replica has its own lock (api 5.2 makes it work
across replicas). ``start`` takes the lock and schedules the run in the background; the route
answers ``202`` at once. The run's outcome is logged and emitted as one bounded event
(``ingest.completed``): counts and seconds, and a success/partial/error outcome with an error
**kind** from a fixed list. It never carries a document's text, a path, or an exception's message —
those can hold service URLs or response bodies — and an unexpected failure is only
``unexpected``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.ai.config import AINotConfigured
from app.ai.ingest import (
    CorpusEmpty,
    CorpusNotFound,
    CorpusTooLarge,
    CorpusUnreadable,
    EmbeddingDimensionMismatch,
    IndexDimensionMismatch,
    IndexHasNoVectorField,
    IngestError,
    IngestReport,
    IngestTimeout,
    ingest_corpus,
)
from app.errors import Conflict
from app.events import track_event
from app.logging_config import get_logger

EVENT_NAME = "ingest.completed"
RUNNING_CODE = "ingest_running"

_log = get_logger("app.ai.ingest_job")

# The bounded set of error kinds; another IngestError is "ingest_error", anything else "unexpected".
_KINDS: tuple[tuple[type[BaseException], str], ...] = (
    (CorpusNotFound, "corpus_not_found"),
    (CorpusEmpty, "corpus_empty"),
    (CorpusTooLarge, "corpus_too_large"),
    (CorpusUnreadable, "corpus_unreadable"),
    (IngestTimeout, "timeout"),
    (IndexDimensionMismatch, "dimension_mismatch"),
    (EmbeddingDimensionMismatch, "dimension_mismatch"),
    (IndexHasNoVectorField, "index_shape"),
    (AINotConfigured, "not_configured"),
    (asyncio.CancelledError, "cancelled"),
)

# The run in flight, if any. Set and cleared on the event loop, with no await between the check and
# the assignment, so two calls cannot both start.
_task: asyncio.Task[None] | None = None


def error_kind(exc: BaseException) -> str:
    """A bounded kind for a failed run — never the exception's text."""
    for cls, kind in _KINDS:
        if isinstance(exc, cls):
            return kind
    return "ingest_error" if isinstance(exc, IngestError) else "unexpected"


def is_running() -> bool:
    return _task is not None and not _task.done()


def outcome_of(report: IngestReport) -> str:
    """``success`` when everything uploaded and nothing failed to be removed, else ``partial``."""
    clean = not (report.failed or report.removal_failed or report.removal_refused)
    return "success" if clean else "partial"


def _report_success(report: IngestReport, seconds: float) -> None:
    outcome = outcome_of(report)
    fields: dict[str, Any] = {
        "outcome": outcome,
        "error_kind": "none",
        "documents": report.documents,
        "chunks": report.chunks,
        "uploaded": report.uploaded,
        "failed": report.failed,
        "removed": report.removed,
        "removal_failed": report.removal_failed,
        "removal_refused": report.removal_refused,
        "seconds": seconds,
    }
    _log.info("ingest finished", **fields)
    track_event(EVENT_NAME, fields)


def _report_failure(exc: BaseException, seconds: float) -> None:
    kind = error_kind(exc)
    fields: dict[str, Any] = {"outcome": "error", "error_kind": kind, "seconds": seconds}
    # `kind` only. No exc_info and no str(exc): either can carry a URL or a response body.
    _log.error("ingest failed", **fields)
    track_event(EVENT_NAME, fields)


async def _execute() -> None:
    """The background run: ``ingest_corpus`` with its outcome reported, never raising."""
    started = time.perf_counter()
    try:
        report = await ingest_corpus()
    except asyncio.CancelledError as exc:
        _safely(_report_failure, exc, round(time.perf_counter() - started, 2))
        raise
    except Exception as exc:  # noqa: BLE001 - a background task has no caller to hand this to
        _safely(_report_failure, exc, round(time.perf_counter() - started, 2))
        return
    _safely(_report_success, report, round(time.perf_counter() - started, 2))


def _safely(report: Any, *args: Any) -> None:
    """Run a reporter; if it fails, say nothing: an exception that escaped the task would be printed
    by asyncio with its message, which the bounded-kind rule exists to keep out of the logs."""
    try:
        report(*args)
    except Exception:  # noqa: BLE001
        pass


def start() -> None:
    """Take the lock and schedule a run; ``409 ingest_running`` when one is already in flight."""
    global _task
    if is_running():
        raise Conflict(RUNNING_CODE)
    _task = asyncio.get_running_loop().create_task(_execute())


def _reset_for_tests() -> None:
    """TEST-ONLY: forget the run in flight."""
    global _task
    _task = None
