"""Conversation + message models (``docs/design/db-design.md``; ARCHITECTURE.md B4).

A ``Conversation`` owns ``Message`` rows. Conversations are shared (no owner column) until auth
lands — ADR-0014. A thread is ordered by ``Message.seq`` (an ``IDENTITY`` column), never by
``created_at``: two messages stored in the same millisecond would tie (decision 12).

The ORM defines no relationships on purpose: the async session cannot lazy-load, so the
repository (``app/repositories/conversations.py``) always loads messages with an explicit query.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Unicode,
    func,
)
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base

DEFAULT_CONVERSATION_TITLE = "New conversation"
TITLE_MAX_LENGTH = 200

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
MESSAGE_ROLES = (ROLE_USER, ROLE_ASSISTANT)


def utcnow() -> datetime:
    """Naive UTC timestamp, truncated to milliseconds.

    ``datetime2(3)`` carries no offset (everything is stored as UTC) and keeps 3 fractional
    digits; truncating here makes the in-memory value equal to what a later read returns.
    ``DATETIME2`` + ``sysutcdatetime()`` deliberately deviate from rule 25's
    ``DateTime(timezone=True)`` suggestion: ``docs/design/db-design.md`` pins ``datetime2(3)``.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(
        Unicode(TITLE_MAX_LENGTH), nullable=False, default=DEFAULT_CONVERSATION_TITLE
    )
    created_at: Mapped[datetime] = mapped_column(
        DATETIME2(3), nullable=False, default=utcnow, server_default=func.sysutcdatetime()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME2(3), nullable=False, default=utcnow, server_default=func.sysutcdatetime()
    )

    __table_args__ = (
        # Newest-first conversation list. Expression index → explicit name (the naming
        # convention cannot derive one from an expression).
        Index("ix_conversations_updated_at", updated_at.desc()),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Thread order (decision 12). Filled by the database; the repository refreshes it after
    # the insert because the async session cannot lazy-load it.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(start=1), nullable=False, unique=True)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="NO ACTION"), nullable=False
    )
    role: Mapped[str] = mapped_column(Unicode(20), nullable=False)
    # `Unicode()` with no length is NVARCHAR(max) offline too — `UnicodeText` would render the
    # deprecated NTEXT whenever the dialect has not connected (e.g. `alembic upgrade --sql`).
    content: Mapped[str] = mapped_column(Unicode(), nullable=False)
    # ``[{ "title": …, "path": … }]`` as JSON text (NVARCHAR(max)); NULL for user messages.
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME2(3), nullable=False, default=utcnow, server_default=func.sysutcdatetime()
    )

    __table_args__ = (
        CheckConstraint(
            "role IN (" + ", ".join(f"'{role}'" for role in MESSAGE_ROLES) + ")", name="role_valid"
        ),
        CheckConstraint("citations IS NULL OR ISJSON(citations) = 1", name="citations_json"),
        # Thread reads: WHERE conversation_id = :id ORDER BY seq.
        Index("ix_messages_conversation_id_seq", "conversation_id", "seq"),
    )
