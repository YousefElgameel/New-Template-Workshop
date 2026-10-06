"""Conversation + message queries (``docs/design/db-design.md``).

Every function takes the request's ``AsyncSession`` and uses ORM constructs only (parameterised).
They **flush but never commit** — the caller owns the unit of work. Nothing here logs: titles
and message text are user content and must never reach logs, spans or metrics.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import (
    DEFAULT_CONVERSATION_TITLE,
    MESSAGE_ROLES,
    ROLE_USER,
    TITLE_MAX_LENGTH,
    Conversation,
    Message,
    utcnow,
)

MIN_LIST_LIMIT = 1
MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 50
MAX_HISTORY_LIMIT = 100
# Most messages one read of a conversation returns (the most recent ones; see `Thread`).
MAX_THREAD_MESSAGES = 500

_WHITESPACE = re.compile(r"\s+")


class ConversationNotFound(LookupError):
    """No conversation with that id (the endpoint maps this to ``404 not_found``)."""


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units — how SQL Server measures ``NVARCHAR(n)`` (an emoji is 2)."""
    return len(text.encode("utf-16-le")) // 2


def _cut_utf16(text: str, max_units: int) -> str:
    """The longest prefix of ``text`` within ``max_units`` UTF-16 units, whole characters only."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > max_units:
            return text[:index]
    return text


def make_title(text: str | None) -> str:
    """A title from free text: whitespace collapsed, cut to what fits ``NVARCHAR(200)``.

    The column counts UTF-16 code units, so 200 emoji would be 400 and overflow it: the cut is by
    units, and never splits a character. Blank or missing text yields the default title.
    """
    collapsed = _WHITESPACE.sub(" ", text or "").strip()
    return _cut_utf16(collapsed, TITLE_MAX_LENGTH).rstrip() or DEFAULT_CONVERSATION_TITLE


async def create_conversation(session: AsyncSession, title: str | None = None) -> Conversation:
    """Insert a conversation (title optional — decision 3) and return it with its timestamps."""
    now = utcnow()
    conversation = Conversation(title=make_title(title), created_at=now, updated_at=now)
    session.add(conversation)
    await session.flush()
    return conversation


async def list_conversations(
    session: AsyncSession, limit: int = DEFAULT_LIST_LIMIT
) -> Sequence[Conversation]:
    """Newest first by ``updated_at``; ``limit`` must be 1-100 (the endpoint answers 422).

    Conversations updated in the same millisecond come back in an arbitrary (id) order.
    """
    if not MIN_LIST_LIMIT <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between {MIN_LIST_LIMIT} and {MAX_LIST_LIMIT}")
    result = await session.execute(
        select(Conversation)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def get_conversation(session: AsyncSession, conversation_id: UUID) -> Conversation | None:
    return await session.get(Conversation, conversation_id)


class Thread(NamedTuple):
    """A conversation with (at most ``MAX_THREAD_MESSAGES`` of) its messages, oldest first."""

    conversation: Conversation
    messages: Sequence[Message]
    truncated: bool  # True when older messages were left out


async def get_conversation_with_messages(
    session: AsyncSession, conversation_id: UUID, limit: int = MAX_THREAD_MESSAGES
) -> Thread | None:
    """The conversation and its thread, ordered by ``seq`` (decision 12), bounded.

    A conversation can grow without limit (no purge job, decision 5), so a read never returns more
    than ``limit`` messages: the MOST RECENT ones, oldest first, with ``truncated`` set when older
    ones were left out. One extra row is fetched to know that without a second query.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return None
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq.desc())
        .limit(limit + 1)
    )
    newest_first = list(result.scalars().all())
    truncated = len(newest_first) > limit
    return Thread(conversation, list(reversed(newest_first[:limit])), truncated)


async def get_recent_messages(
    session: AsyncSession, conversation_id: UUID, limit: int = 10
) -> Sequence[Message]:
    """The last ``limit`` messages, oldest first — the history passed to the model (decision 4)."""
    if not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_HISTORY_LIMIT}")
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def add_message(
    session: AsyncSession,
    conversation_id: UUID,
    role: str,
    content: str,
    *,
    citations: list[dict[str, Any]] | None = None,
    token_count: int | None = None,
) -> Message:
    """Append a message to a conversation.

    Bumps the conversation's ``updated_at`` (decision 11). A user message on a conversation
    still titled "New conversation" also becomes its title (decision 3) — the rule is "the title
    is still the default", so a blank first question leaves it for the next one. Raises
    ``ConversationNotFound`` for an unknown id and ``ValueError`` for an invalid role.

    Keep one flush per message: ``seq`` is an IDENTITY value and a multi-row insert does not
    guarantee it follows row order.
    """
    if role not in MESSAGE_ROLES:
        raise ValueError(f"role must be one of {MESSAGE_ROLES}")
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFound(str(conversation_id))

    now: datetime = utcnow()
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        citations=citations,
        token_count=token_count,
        created_at=now,
    )
    session.add(message)
    conversation.updated_at = now
    if role == ROLE_USER and conversation.title == DEFAULT_CONVERSATION_TITLE:
        conversation.title = make_title(content)
    await session.flush()
    # ``seq`` is generated by the database (IDENTITY); an async session cannot lazy-load it.
    await session.refresh(message, attribute_names=["seq"])
    return message
