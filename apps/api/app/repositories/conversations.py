"""Conversation + message queries (``docs/design/db-design.md``).

Every function takes the request's ``AsyncSession`` and uses ORM constructs only (parameterised).
They **flush but never commit** — the caller owns the unit of work. Nothing here logs: titles
and message text are user content and must never reach logs, spans or metrics.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple, Protocol
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
# About how many bytes of message text one read of a conversation returns (api 5.4). The web BFF
# refuses an upstream body over 8 MiB, and 500 messages of 4,000 four-byte characters with long
# citation titles can reach about 10 MB; this keeps the answer well under that.
MAX_THREAD_BYTES = 4 * 1024 * 1024
# What a message costs on the wire besides its text: ids, role, timestamp and the JSON keys.
MESSAGE_OVERHEAD_BYTES = 256

_WHITESPACE = re.compile(r"\s+")


class ConversationNotFound(LookupError):
    """No conversation with that id (the endpoint maps this to ``404 not_found``)."""


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units — how SQL Server measures ``NVARCHAR(n)`` (an emoji is 2)."""
    return len(text.encode("utf-16-le")) // 2


class _HasText(Protocol):
    """What the byte count reads from a message: its text and its citations."""

    content: str
    citations: Any


def message_wire_bytes(message: _HasText) -> int:
    """About how many bytes a message takes in the response: the UTF-8 length of its content and
    citations as JSON (quotes and control characters are escaped, so they count double) plus a
    fixed allowance for the rest. Measured on bytes, not characters: an emoji is 4 bytes. It never
    counts fewer bytes than the response holds (the allowance is more than the ~150 bytes of ids,
    role, timestamp and keys). A lone surrogate counts 3 bytes here and still fails when the
    response is written, as it did before: ask cleans them out on every write."""
    text = json.dumps(
        [message.content, message.citations], ensure_ascii=False, separators=(",", ":")
    )
    return len(text.encode("utf-8", "surrogatepass")) + MESSAGE_OVERHEAD_BYTES


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
    """A conversation with (at most ``MAX_THREAD_MESSAGES`` / ``MAX_THREAD_BYTES`` of) its messages,
    oldest first."""

    conversation: Conversation
    messages: Sequence[Message]
    truncated: bool  # True when older messages were left out (by count or by bytes)


async def get_conversation_with_messages(
    session: AsyncSession,
    conversation_id: UUID,
    limit: int = MAX_THREAD_MESSAGES,
    max_bytes: int = MAX_THREAD_BYTES,
) -> Thread | None:
    """The conversation and its thread, ordered by ``seq`` (decision 12), bounded twice.

    A conversation can grow without limit (no purge job, decision 5), so a read never returns more
    than ``limit`` messages, nor about more than ``max_bytes`` bytes of them: the MOST RECENT ones,
    oldest first, with ``truncated`` set when older ones were left out. One extra row is fetched to
    know about the count without a second query; the byte bound is worked out on the rows already
    read, newest first, so it reads nothing more. The newest message is always returned, even over
    ``max_bytes`` (an empty thread would be worse): the bound is hard only because a message is
    capped on write (4,000 characters, ``ask.py``); a longer one stored by other means is returned
    whole.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")
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
    kept: list[Message] = []
    used = 0
    for message in newest_first[:limit]:
        size = message_wire_bytes(message)
        if kept and used + size > max_bytes:
            truncated = True
            break
        kept.append(message)
        used += size
    return Thread(conversation, list(reversed(kept)), truncated)


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
