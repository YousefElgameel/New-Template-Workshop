"""The ingest lock shared by every replica (api roadmap 5.2; decision 19).

``app.ai.ingest_job`` keeps one run at a time **in this process**; this is what keeps it to one run
at a time **across replicas**. It is a row in the project database (``app/models/ingest_lock.py``,
statements in ``app/repositories/ingest_lock.py``): taken when a run starts, pushed forward every
``ttl / 3`` seconds while it runs, deleted when it ends. A replica that crashes stops renewing,
so its row **expires on its own** after the ``ttl`` and any replica may then take it over.

It is used when ``DATABASE_URL`` is configured (``database_is_configured``). A local run with no
database (``APP_ENV=local``) keeps the in-process lock alone; a deployed one with no database URL
fails closed, as does a configured database that cannot say (the migration was not applied, the
database is unreachable, a call took too long): ``POST /v1/admin/ingest`` answers
``503 ingest_lock_unavailable`` rather than risk two runs at once. Every database call here has a
time limit. Nothing here logs a value from the database or the exception's message, only its kind.

Outside a request there is no ``Depends(get_session)``: each operation opens one short-lived
session from the lazy factory (``async with get_sessionmaker()() as session``) and closes it; none
is held across operations or shared with another task.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import PLACEHOLDER_DATABASE_HOST, get_settings
from app.db import get_sessionmaker
from app.errors import ApiError
from app.logging_config import get_logger
from app.repositories import ingest_lock as repo

# How long a lock is good for without a renewal: a run that dies leaves the lock this long.
DEFAULT_TTL_SECONDS = 300
# The holder renews this many times in one ttl, so a missed heartbeat or two does not lose the lock.
RENEWALS_PER_TTL = 3
# Time limits of the database calls (a hung connection must not park the caller): taking the lock
# (inside a request), one renewal (it must end well before the next heartbeat), giving it back.
ACQUIRE_TIMEOUT_SECONDS = 15.0
RENEW_TIMEOUT_SECONDS = 20.0
RELEASE_TIMEOUT_SECONDS = 5.0
# After a failed renewal the next try comes sooner than a whole heartbeat.
RETRY_SECONDS = 10.0

_log = get_logger("app.ai.ingest_lock")


class IngestLockUnavailable(ApiError):
    """The cross-replica lock cannot be taken or checked (bounded; no database text)."""

    status_code = 503
    code = "ingest_lock_unavailable"


def database_is_configured() -> bool:
    """False only for the placeholder ``DATABASE_URL`` the service boots with (an unset or empty
    variable is the placeholder too). A URL that cannot be parsed counts as configured: the lock
    then fails closed instead of silently going without."""
    try:
        host = make_url(get_settings().database_url).host
    except Exception:  # noqa: BLE001 - kind-only below, at the point of use
        return True
    return host != PLACEHOLDER_DATABASE_HOST


class DatabaseIngestLock:
    """One acquisition: ``acquire``, ``renew`` every ``heartbeat_seconds``, then ``release``."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        if ttl_seconds < RENEWALS_PER_TTL:
            raise ValueError("ttl_seconds is too short to renew inside it")
        self.ttl_seconds = ttl_seconds
        self.heartbeat_seconds = ttl_seconds / RENEWALS_PER_TTL
        # One renewal must be over (or given up) long before the next heartbeat is due.
        self.renew_timeout_seconds = min(RENEW_TIMEOUT_SECONDS, self.heartbeat_seconds)
        self.holder: UUID | None = None

    async def acquire(self) -> bool:
        """True when this replica now holds the lock, False when another one does.

        Raises ``IngestLockUnavailable`` when the database cannot say (fail closed): an error, a
        call that takes longer than ``ACQUIRE_TIMEOUT_SECONDS``, or a row that is not the primary
        key violation it seems to be. A deadlock victim or a dropped connection is tried once more.
        If the caller is cancelled while it waits, ``holder`` is set when the row may have been
        written, so ``release`` can still give it back."""
        holder = uuid4()
        for attempt in (1, 2):
            try:
                async with asyncio.timeout(ACQUIRE_TIMEOUT_SECONDS):
                    return await self._try(holder)
            except IngestLockUnavailable:
                self.holder = None
                raise
            except OperationalError as exc:
                if attempt == 1:
                    _log.warning("ingest lock retry", step="acquire", kind=type(exc).__name__)
                    continue
                self._give_up(exc)
            except Exception as exc:  # noqa: BLE001 - kind only; fail closed, whatever it was
                self._give_up(exc)
        raise AssertionError("unreachable")  # pragma: no cover

    def _give_up(self, exc: BaseException) -> None:
        self.holder = None
        _log.error("ingest lock unavailable", step="acquire", kind=type(exc).__name__)
        raise IngestLockUnavailable() from None

    async def _try(self, holder: UUID) -> bool:
        self.holder = holder  # before the commit: a cancel after it must still find the row
        async with get_sessionmaker()() as session:
            if not await repo.take_over_expired(session, holder, self.ttl_seconds):
                try:
                    await repo.insert_lock(session, holder, self.ttl_seconds)
                except IntegrityError as exc:
                    await session.rollback()
                    if await repo.lock_exists(session):
                        self.holder = None
                        return False  # the row exists and has not expired: somebody holds it
                    # An integrity error with no row behind it is not "held": do not hide it.
                    _log.error("ingest lock unavailable", step="insert", kind=type(exc).__name__)
                    raise IngestLockUnavailable() from None
            await session.commit()
        return True

    async def renew(self) -> bool:
        """True while this holder still owns the row; False when it was lost (expired and taken).

        A database error is raised: the caller decides how long it can live without a renewal
        (and gives each renewal ``renew_timeout_seconds``)."""
        if self.holder is None:
            return False
        async with get_sessionmaker()() as session:
            owned = await repo.renew(session, self.holder, self.ttl_seconds)
            await session.commit()
        return owned

    async def release(self) -> None:
        """Give the lock back. Never raises, and never waits longer than
        ``RELEASE_TIMEOUT_SECONDS``: if it cannot, the row expires by itself."""
        holder, self.holder = self.holder, None
        if holder is None:
            return
        try:
            async with asyncio.timeout(RELEASE_TIMEOUT_SECONDS):
                async with get_sessionmaker()() as session:
                    await repo.release(session, holder)
                    await session.commit()
        except Exception as exc:  # noqa: BLE001 - a lock that cannot be released expires by itself
            _log.warning("ingest lock not released", kind=type(exc).__name__)
