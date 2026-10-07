"""Ask a question in a conversation (``POST /v1/conversations/{id}/ask``) — roadmap api 4.2.

The answer comes from the team's docs (retrieval → model, ``app.ai.graph``), with the conversation's
last messages as history. The order of events is part of the contract:

1. the body is checked (blank → ``422``, over 4,000 characters → ``413``) and the conversation must
   exist (``404``) — nothing has been stored yet;
2. a slot is taken, or the call is refused with ``429 busy`` (``app.ai.limits``) — nothing stored;
3. the last 10 messages are read as history, **then** the user's message is stored and committed, so
   it is kept whatever happens next;
4. the graph runs; any model, search or configuration failure (or a blank answer) becomes
   ``503 ai_unavailable`` and **no assistant message is stored**;
5. the answer is capped, its citations are taken from the passages it cites (``app.ai.citations`` —
   never from text the model wrote), and the assistant message is stored and committed.

Everything here is user content or model output: no question, answer, passage, title or id reaches a
log line, a span or a metric. Logs carry counts, booleans and a bounded error kind.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.citations import citation_of, cited_chunks
from app.ai.config import get_ai_settings
from app.ai.graph import ask, build_graph
from app.ai.history import HISTORY_MESSAGES, history_messages
from app.ai.limits import NoFreeSlot, ask_slot
from app.ai.telemetry import error_kind
from app.db import get_session
from app.errors import AIUnavailable, ApiError, ErrorBody, NotFound
from app.logging_config import get_logger
from app.models.conversation import ROLE_ASSISTANT, ROLE_USER
from app.repositories import conversations as repo
from app.routers.conversations import Citation

router = APIRouter(prefix="/conversations", tags=["conversations"])
DbSession = Annotated[AsyncSession, Depends(get_session)]
_log = get_logger("app.ask")

MAX_QUESTION_CHARS = 4_000
# A stored answer is cut here, at the same length as a question. Together with `clean_text`
# (control characters other than tab/newline/CR would cost 6 bytes each as JSON escapes) no
# stored character costs more than 4 bytes in a thread read, so ordinary threads of 500
# messages fit the BFF's 8 MiB. A thread of maximum-length emoji-only messages with long
# citation titles can still exceed it — bounding a thread read by bytes is api roadmap 5.4.
MAX_ANSWER_CHARS = 4_000
_ELLIPSIS = "…"


def clean_text(value: str) -> str:
    """``value`` as safe stored text: NUL and lone surrogates dropped, and every control character
    other than tab, newline and carriage return replaced by a space. (A control character is a
    6-byte ``\\u00XX`` escape in JSON, 1 byte of storage; a lone surrogate cannot be encoded.)"""
    out: list[str] = []
    for char in value:
        code = ord(char)
        if code == 0 or 0xD800 <= code <= 0xDFFF:
            continue
        out.append(" " if code < 0x20 and char not in "\n\r\t" else char)
    return "".join(out)


def _a_usable_question(value: str) -> str:
    """Trimmed, non-blank, and something the database and the model can take as text."""
    text = value.strip()
    if not text:
        raise ValueError("the question is blank")
    if "\x00" in text:
        raise ValueError("the question contains a NUL character")
    try:
        text.encode("utf-8")  # a lone surrogate (from a JSON \ud800 escape) cannot be encoded
    except UnicodeEncodeError as exc:
        raise ValueError("the question is not valid text") from exc
    text = clean_text(text).strip()
    if not text:
        raise ValueError("the question is blank")
    return text


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No max_length here: a too-long question is a 413, not a 422 (checked in the route).
    question: Annotated[str, AfterValidator(_a_usable_question)] = Field(
        description=f"The question. Not blank; at most {MAX_QUESTION_CHARS:,} characters."
    )


class AskOut(BaseModel):
    message_id: UUID = Field(description="The stored assistant message.")
    answer: str
    citations: list[Citation] = Field(
        description=(
            "The retrieved documents the answer cites, each once, in citation order; empty when "
            "it cites none. Titles and paths are untrusted text: show them as plain text."
        )
    )


_graph: Any | None = None


def get_answer_graph() -> Any:
    """The compiled retrieve → answer graph (built once; nothing connects until the first ask).
    A dependency so tests can swap in a graph with a fake model and retriever."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _reset_for_tests() -> None:
    global _graph
    _graph = None


AnswerGraph = Annotated[Any, Depends(get_answer_graph)]


def cap_answer(text: str) -> tuple[str, bool]:
    """``text`` cut to ``MAX_ANSWER_CHARS`` characters (ending in "…"), and whether it was cut."""
    if len(text) <= MAX_ANSWER_CHARS:
        return text, False
    return text[: MAX_ANSWER_CHARS - len(_ELLIPSIS)].rstrip() + _ELLIPSIS, True


@router.post(
    "/{conversation_id}/ask",
    response_model=AskOut,
    responses={
        404: {"model": ErrorBody, "description": "No such conversation (`not_found`)"},
        413: {"model": ErrorBody, "description": "The question is too long (`payload_too_large`)"},
        429: {"model": ErrorBody, "description": "Too many asks at once (`busy`)"},
        503: {
            "model": ErrorBody,
            "description": (
                "The model or the search did not answer, or the AI layer is not configured "
                "(`ai_unavailable`). The question is kept; no answer is stored."
            ),
        },
    },
    summary="Ask a question; the answer cites the docs",
)
async def ask_question(
    conversation_id: UUID, body: AskIn, session: DbSession, graph: AnswerGraph
) -> AskOut:
    question = body.question
    if len(question) > MAX_QUESTION_CHARS:
        raise ApiError("payload_too_large", status_code=413)
    if await repo.get_conversation(session, conversation_id) is None:
        raise NotFound()

    try:
        with ask_slot(get_ai_settings().max_concurrent_asks):
            return await _ask(conversation_id, question, session, graph)
    except NoFreeSlot:
        raise ApiError("busy", status_code=429) from None


async def _ask(conversation_id: UUID, question: str, session: AsyncSession, graph: Any) -> AskOut:
    # History first: read before the new question is stored, so it never contains it.
    history = history_messages(
        await repo.get_recent_messages(session, conversation_id, HISTORY_MESSAGES)
    )

    # The question is stored and committed before the model is called, so it survives a failure.
    try:
        await repo.add_message(session, conversation_id, ROLE_USER, question)
    except repo.ConversationNotFound:
        raise NotFound() from None
    await session.commit()

    try:
        answer = await ask(graph, question, history=history)
    except Exception as exc:  # noqa: BLE001 - the client gets a bounded code, never the message
        _log.warning("ask failed", kind=error_kind(exc))
        raise AIUnavailable() from None

    text = clean_text(answer.text)
    if not text.strip():
        _log.warning("ask failed", kind="empty_answer")
        raise AIUnavailable()
    text, was_cut = cap_answer(text)
    # Citations: the retrieved passages the (stored) answer cites — never anything the model wrote.
    citations = [citation_of(chunk) for chunk in cited_chunks(text, answer.context)]
    tokens = answer.output_tokens if isinstance(answer.output_tokens, int) else None

    message = await repo.add_message(
        session,
        conversation_id,
        ROLE_ASSISTANT,
        text,
        citations=citations,
        token_count=tokens if tokens is not None and tokens >= 0 else None,
    )
    await session.commit()
    _log.info("question answered", history=len(history), citations=len(citations), cut=was_cut)
    return AskOut(message_id=message.id, answer=text, citations=[Citation(**c) for c in citations])
