"""The lock that lets only one replica run an ingestion at a time (api roadmap 5.2).

One row per lock name (today only ``ingest``). A replica takes the lock by inserting the row, or by
taking over a row whose ``expires_at`` has passed; it keeps it by pushing ``expires_at`` forward
while it runs, and gives it back by deleting the row. A replica that crashes stops renewing, so
the row expires on its own. All times are the database's own clock (``SYSUTCDATETIME()``), never a
replica's, so clock drift between replicas cannot matter. The row holds no content: a name, a random
holder id and two UTC instants.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Unicode
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

LOCK_NAME_MAX_LENGTH = 64


class IngestLock(Base):
    __tablename__ = "ingest_locks"

    name: Mapped[str] = mapped_column(Unicode(LOCK_NAME_MAX_LENGTH), primary_key=True)
    # A fresh random id per acquisition: only the holder can renew or release its own row.
    holder: Mapped[UUID] = mapped_column(nullable=False)
    # No server default (unlike the conversation tables): every write sets both instants from the
    # database clock itself, in the statement (``app/repositories/ingest_lock.py``).
    acquired_at: Mapped[datetime] = mapped_column(DATETIME2(3), nullable=False)
    # After this instant any replica may take the lock over (a crashed holder never renews).
    expires_at: Mapped[datetime] = mapped_column(DATETIME2(3), nullable=False)
