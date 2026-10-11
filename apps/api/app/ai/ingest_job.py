"""The ingestion run behind ``POST /v1/admin/ingest`` (api roadmap 3.3; decision 19).

One run at a time, in this process **and across replicas**: ``start`` first checks this process,
then takes the database lock (``app/ai/ingest_lock.py``, api 5.2) when ``DATABASE_URL`` is
configured, and schedules the run in the background; the route answers ``202`` at once. While the
run goes the lock is renewed every ``ttl / 3`` seconds; a run that loses it (it expired and another
replica took it over) is stopped, and a replica that dies leaves a lock that expires by itself. A
run also has an overall deadline (``INGEST_MAX_RUN_SECONDS``): a wedged run does not hold the lock
for ever just because it keeps renewing it.

The run's outcome is logged and emitted as one bounded event (``ingest.completed``): counts and
seconds, and a success/partial/error outcome with an error **kind** from a fixed list. It never
carries a document's text, a path, or an exception's message — those can hold service URLs or
response bodies — and an unexpected failure is only ``unexpected``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.ai.config import AINotConfigured, get_ai_settings
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
from app.ai.ingest_lock import (
    RETRY_SECONDS,
    DatabaseIngestLock,
    IngestLockUnavailable,
    database_is_configured,
)
from app.config import get_settings
from app.errors import Conflict
from app.events import track_event
from app.logging_config import get_logger

EVENT_NAME = "ingest.completed"
RUNNING_CODE = "ingest_running"

_log = get_logger("app.ai.ingest_job")


class IngestLockLost(RuntimeError):
    """The run's lock expired and was taken over (no renewal in time): the run was stopped."""


# The bounded set of error kinds; another IngestError is "ingest_error", anything else "unexpected".
_KINDS: tuple[tuple[type[BaseException], str], ...] = (
    (CorpusNotFound, "corpus_not_found"),
    (CorpusEmpty, "corpus_empty"),
    (CorpusTooLarge, "corpus_too_large"),
    (CorpusUnreadable, "corpus_unreadable"),
    (IngestTimeout, "timeout"),
    (TimeoutError, "timeout"),  # the overall deadline of the run
    (IndexDimensionMismatch, "dimension_mismatch"),
    (EmbeddingDimensionMismatch, "dimension_mismatch"),
    (IndexHasNoVectorField, "index_shape"),
    (AINotConfigured, "not_configured"),
    (IngestLockLost, "lock_lost"),
    (asyncio.CancelledError, "cancelled"),
)

# The run in flight in this process, if any. Two calls in this process cannot both start: with no
# database there is no await between the check in ``start`` and the assignment; with one, the
# second call's ``acquire`` loses to the first's row and is a 409.
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


def new_lock() -> DatabaseIngestLock | None:
    """The cross-replica lock; ``None`` only for a local run with no database.

    A deployed environment (``APP_ENV`` not ``local``) with no database URL fails closed: it
    may be one of several replicas, and an unset URL must not quietly drop the lock."""
    if database_is_configured():
        return DatabaseIngestLock()
    if get_settings().app_env != "local":
        _log.error("ingest lock unavailable", step="configure", kind="no_database_url")
        raise IngestLockUnavailable()
    return None


async def _keep_alive(
    lock: DatabaseIngestLock, run: asyncio.Task[Any], lost: asyncio.Event
) -> None:
    """Renew ``lock`` every heartbeat while ``run`` goes; stop ``run`` when the lock is lost.

    Lost means the row is no longer ours, or no renewal has succeeded for ``ttl - heartbeat``
    (the database cannot be reached or is too slow). That is **before** the row expires, so by the
    time another replica can take the lock over this run has already been told to stop. A failed
    renewal is tried again after ``RETRY_SECONDS`` rather than a whole heartbeat, and each renewal
    has ``lock.renew_timeout_seconds``: a hung connection counts as a failed renewal."""
    give_up_after = lock.ttl_seconds - lock.heartbeat_seconds
    last_ok = time.monotonic()  # the lock was taken just now
    delay = lock.heartbeat_seconds
    while True:
        await asyncio.sleep(delay)
        attempt_started = time.monotonic()  # the expiry it sets is at least this late
        try:
            async with asyncio.timeout(lock.renew_timeout_seconds):
                owned: bool | None = await lock.renew()
        except Exception as exc:  # noqa: BLE001 - kind only; the lock may still be good
            _log.warning("ingest lock renewal failed", kind=type(exc).__name__)
            owned = None
        if owned:
            last_ok = attempt_started
            delay = lock.heartbeat_seconds
        elif owned is False or time.monotonic() - last_ok >= give_up_after:
            lost.set()
            run.cancel()
            return
        else:
            delay = min(lock.heartbeat_seconds, RETRY_SECONDS)


def _beat_ended(beat: asyncio.Task[None], run: asyncio.Task[Any], lost: asyncio.Event) -> None:
    """If the heartbeat dies on an unexpected error nothing renews the lock: stop the run."""
    if beat.cancelled() or beat.exception() is None:
        return
    _log.error("ingest lock heartbeat died", kind=type(beat.exception()).__name__)
    lost.set()
    run.cancel()


async def _ingest_within_deadline() -> IngestReport:
    async with asyncio.timeout(get_ai_settings().ingest_max_run_seconds):
        return await ingest_corpus()


async def _run(lock: DatabaseIngestLock | None) -> IngestReport:
    """``ingest_corpus`` under its deadline, kept under ``lock``'s heartbeat when there is one."""
    if lock is None:
        return await _ingest_within_deadline()
    lost = asyncio.Event()
    run = asyncio.create_task(_ingest_within_deadline())
    beat = asyncio.create_task(_keep_alive(lock, run, lost))
    beat.add_done_callback(lambda task: _beat_ended(task, run, lost))
    try:
        return await run
    except asyncio.CancelledError:
        if lost.is_set():  # we stopped it: that is a failure to report, not a cancellation
            raise IngestLockLost from None
        raise
    finally:
        run.cancel()
        beat.cancel()
        await asyncio.gather(run, beat, return_exceptions=True)


async def _execute(lock: DatabaseIngestLock | None = None) -> None:
    """The background run: ``ingest_corpus`` with its outcome reported, never raising.

    ``lock`` (already taken by ``start``) is given back however the run ends; the release is
    shielded so the one cancellation that ends the run cannot cut it short (it has its own time
    limit)."""
    started = time.perf_counter()
    try:
        report = await _run(lock)
    except asyncio.CancelledError as exc:
        _safely(_report_failure, exc, round(time.perf_counter() - started, 2))
        raise
    except Exception as exc:  # noqa: BLE001 - a background task has no caller to hand this to
        _safely(_report_failure, exc, round(time.perf_counter() - started, 2))
        return
    finally:
        if lock is not None:
            await asyncio.shield(lock.release())
    _safely(_report_success, report, round(time.perf_counter() - started, 2))


def _safely(report: Any, *args: Any) -> None:
    """Run a reporter; if it fails, say nothing: an exception that escaped the task would be printed
    by asyncio with its message, which the bounded-kind rule exists to keep out of the logs."""
    try:
        report(*args)
    except Exception:  # noqa: BLE001
        pass


async def start() -> None:
    """Take the locks and schedule a run; ``409 ingest_running`` when one is already in flight in
    this process or, with a database, in any replica (``503 ingest_lock_unavailable`` when the
    database cannot say)."""
    global _task
    if is_running():
        raise Conflict(RUNNING_CODE)
    lock = new_lock()
    if lock is not None:
        try:
            acquired = await lock.acquire()
        except asyncio.CancelledError:  # the row may have been written: give it back
            await asyncio.shield(lock.release())
            raise
        if not acquired:
            raise Conflict(RUNNING_CODE)
    try:
        _task = asyncio.get_running_loop().create_task(_execute(lock))
    except BaseException:
        if lock is not None:
            await lock.release()
        raise


def _reset_for_tests() -> None:
    """TEST-ONLY: forget the run in flight."""
    global _task
    _task = None
