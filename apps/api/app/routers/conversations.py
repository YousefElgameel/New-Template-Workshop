"""Conversations: create, list and open (``/v1/conversations``) — roadmap api 2.1.

Conversations are shared (no owner) until auth lands — ADR-0014. Everything here is user content
(titles, message text, citations): none of it, and no conversation id, is logged, put on a span or
used as a metric attribute. Metrics label by the route PATTERN (``/v1/conversations/{id}``); the
request span's URL attributes are scrubbed of UUIDs by ``app.observability.scrub_ids_server_hook``.

The repository flushes and never commits; this module owns the unit of work, so the one write
(``POST``) commits explicitly and the reads never do.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ErrorBody, NotFound
from app.logging_config import get_logger
from app.models.conversation import TITLE_MAX_LENGTH
from app.repositories import conversations as repo

router = APIRouter(prefix="/conversations", tags=["conversations"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
_log = get_logger("app.conversations")


def _as_utc(value: datetime) -> datetime:
    """Stored as naive UTC (``datetime2`` has no offset); returned as an explicit-UTC instant."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


def _fits_the_title_column(value: str | None) -> str | None:
    """``NVARCHAR(200)`` counts UTF-16 code units: 150 emoji would be 300 units and overflow."""
    if value is not None and repo.utf16_length(value) > TITLE_MAX_LENGTH:
        raise ValueError(f"title is longer than {TITLE_MAX_LENGTH} characters")
    return value


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str | None, AfterValidator(_fits_the_title_column)] = Field(
        default=None,
        max_length=TITLE_MAX_LENGTH,
        description=(
            'Optional. Blank or missing → "New conversation", replaced by the first question.'
        ),
    )


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ConversationListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    updated_at: UtcDatetime


class ConversationList(BaseModel):
    items: list[ConversationListItem]
    count: int = Field(description="Number of items returned (not a total).")


class Citation(BaseModel):
    title: str
    path: str


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: Literal["user", "assistant"]
    content: str
    citations: list[Citation] | None = Field(
        default=None, description="Documents the answer cites; null for user messages."
    )
    created_at: UtcDatetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = Field(description="Oldest first, ordered by thread position.")
    truncated: bool = Field(
        description=(
            f"True when the conversation holds more than {repo.MAX_THREAD_MESSAGES} messages and "
            "only the most recent ones are returned."
        )
    )


@router.post(
    "",
    status_code=201,
    response_model=ConversationOut,
    summary="Start a conversation",
)
async def create_conversation(
    session: DbSession, body: ConversationCreate | None = None
) -> ConversationOut:
    conversation = await repo.create_conversation(session, body.title if body else None)
    await session.commit()
    _log.info("conversation created")
    return ConversationOut.model_validate(conversation)


@router.get("", response_model=ConversationList, summary="List conversations, newest first")
async def list_conversations(
    session: DbSession,
    limit: Annotated[
        int, Query(ge=repo.MIN_LIST_LIMIT, le=repo.MAX_LIST_LIMIT, description="1-100.")
    ] = repo.DEFAULT_LIST_LIMIT,
) -> ConversationList:
    rows = await repo.list_conversations(session, limit)
    _log.info("conversations listed", count=len(rows))
    return ConversationList(
        items=[ConversationListItem.model_validate(row) for row in rows], count=len(rows)
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetail,
    responses={404: {"model": ErrorBody, "description": "No such conversation (`not_found`)"}},
    summary="Open a conversation with its messages",
)
async def get_conversation(conversation_id: UUID, session: DbSession) -> ConversationDetail:
    # The cap is passed explicitly: a default argument would be frozen when the module loads.
    thread = await repo.get_conversation_with_messages(
        session, conversation_id, limit=repo.MAX_THREAD_MESSAGES
    )
    if thread is None:
        raise NotFound()
    _log.info("conversation opened", messages=len(thread.messages), truncated=thread.truncated)
    return ConversationDetail(
        **ConversationOut.model_validate(thread.conversation).model_dump(),
        messages=[MessageOut.model_validate(message) for message in thread.messages],
        truncated=thread.truncated,
    )
