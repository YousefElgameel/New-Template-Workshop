"""Conversation + message queries (``docs/design/db-design.md``).

Every function takes the request's ``AsyncSession`` and uses ORM constructs only (parameterised).
They **flush but never commit** — the caller owns the unit of work. Nothing here logs: titles
and message text are user content and must never reach logs, spans or metrics.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any
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

_WHITESPACE = re.compile(r"\s+")


class ConversationNotFound(LookupError):
    """No conversation with that id (the endpoint maps this to ``404 not_found``)."""


def make_title(text: str | None) -> str:
    """A title from free text: whitespace collapsed, cut to 200 characters.

    Blank or missing text yields the default title.
    """
    collapsed = _WHITESPACE.sub(" ", text or "").strip()
    return collapsed[:TITLE_MAX_LENGTH] or DEFAULT_CONVERSATION_TITLE


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


async def get_conversation_with_messages(
    session: AsyncSession, conversation_id: UUID
) -> tuple[Conversation, Sequence[Message]] | None:
    """The conversation and its whole thread, ordered by ``seq`` (decision 12)."""
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return None
    result = await session.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return conversation, result.scalars().all()


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
