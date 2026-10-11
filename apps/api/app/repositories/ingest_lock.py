"""The statements behind the ingest lock (``app/models/ingest_lock.py``; api roadmap 5.2).

ORM constructs only (parameterised), and every time is the database's own clock. They **flush but
never commit**: the caller (``app.ai.ingest_lock``) owns the transaction, because a lock is only
taken when its transaction commits.

Taking the lock is two statements that are each atomic on one row:

1. ``UPDATE`` the row if it has **expired** (a crashed holder): one row changed means this holder
   took it over. Two replicas racing for an expired row cannot both win: the second ``UPDATE``
   waits for the first, re-reads ``expires_at`` (now in the future) and changes nothing.
2. Otherwise ``INSERT`` the row: the primary key on ``name`` refuses a second one
   (``IntegrityError``), which means somebody holds it (the caller checks the row is there).

It relies on what every row-level write on SQL Server gives: a second ``UPDATE`` of a row waits for
the first and re-evaluates its ``WHERE`` on the committed row, under locking READ COMMITTED and
under Azure SQL's default read-committed snapshot alike. It is worth one run against the dev
database with two replicas racing for an expired row (the roadmap item's ``how_to_test``).
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, insert, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.ingest_lock import IngestLock

LOCK_NAME = "ingest"


def _database_now_plus(seconds: float) -> ColumnElement[Any]:
    """The database's own clock plus ``seconds`` (never a time from this machine)."""
    return func.dateadd(literal_column("SECOND"), seconds, func.sysutcdatetime())


def take_over_expired_statement(holder: UUID, ttl_seconds: float, name: str = LOCK_NAME):
    return (
        update(IngestLock)
        .where(IngestLock.name == name, IngestLock.expires_at < func.sysutcdatetime())
        .values(
            holder=holder,
            acquired_at=func.sysutcdatetime(),
            expires_at=_database_now_plus(ttl_seconds),
        )
    )


def insert_statement(holder: UUID, ttl_seconds: float, name: str = LOCK_NAME):
    return insert(IngestLock).values(
        name=name,
        holder=holder,
        acquired_at=func.sysutcdatetime(),
        expires_at=_database_now_plus(ttl_seconds),
    )


def renew_statement(holder: UUID, ttl_seconds: float, name: str = LOCK_NAME):
    # Only the holder, and only while the row is still its own (not expired and taken over).
    return (
        update(IngestLock)
        .where(IngestLock.name == name, IngestLock.holder == holder)
        .values(expires_at=_database_now_plus(ttl_seconds))
    )


def release_statement(holder: UUID, name: str = LOCK_NAME):
    return delete(IngestLock).where(IngestLock.name == name, IngestLock.holder == holder)


def exists_statement(name: str = LOCK_NAME):
    return select(IngestLock.name).where(IngestLock.name == name)


def _changed(result: Any) -> int:
    return cast("CursorResult[Any]", result).rowcount


async def take_over_expired(
    session: AsyncSession, holder: UUID, ttl_seconds: float, name: str = LOCK_NAME
) -> bool:
    """True when an expired lock was taken over by ``holder``."""
    result = await session.execute(take_over_expired_statement(holder, ttl_seconds, name))
    return _changed(result) == 1


async def insert_lock(
    session: AsyncSession, holder: UUID, ttl_seconds: float, name: str = LOCK_NAME
) -> None:
    """Insert the row; ``IntegrityError`` (the primary key) when it is already there."""
    await session.execute(insert_statement(holder, ttl_seconds, name))


async def lock_exists(session: AsyncSession, name: str = LOCK_NAME) -> bool:
    """Whether the row is there (after a failed insert, to tell it from another integrity error)."""
    return (await session.execute(exists_statement(name))).first() is not None


async def renew(
    session: AsyncSession, holder: UUID, ttl_seconds: float, name: str = LOCK_NAME
) -> bool:
    """True when ``holder`` still owns the row and its expiry was pushed forward."""
    result = await session.execute(renew_statement(holder, ttl_seconds, name))
    return _changed(result) == 1


async def release(session: AsyncSession, holder: UUID, name: str = LOCK_NAME) -> bool:
    """True when ``holder``'s row was removed (False: it expired and was taken over, or is gone)."""
    result = await session.execute(release_statement(holder, name))
    return _changed(result) == 1
