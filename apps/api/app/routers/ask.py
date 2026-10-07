"""Ask a question in a conversation — roadmap api 4.2 (JSON) and 4.3 (stream).

``POST /v1/conversations/{id}/ask`` answers in one JSON body; ``.../ask/stream`` streams the same
answer as server-sent events: ``sources``, then ``token`` frames, then ``done`` or ``error``. Both
follow the same order of events, which is part of the contract:

1. the body is checked (blank → ``422``, over 4,000 characters → ``413``) and the conversation must
   exist (``404``) — nothing has been stored yet;
2. a slot is taken, or the call is refused with ``429 busy`` (``app.ai.limits``) — nothing stored;
3. the last 10 messages are read as history, **then** the user's message is stored and committed, so
   it is kept whatever happens next;
4. the graph runs; any model, search or configuration failure (or a blank answer) becomes
   ``503 ai_unavailable`` and **no assistant message is stored**;
5. the answer is capped, its citations are taken from the passages it cites (``app.ai.citations`` —
   never from text the model wrote), and the assistant message is stored and committed.

For the stream, steps 1-3 happen before the first byte, so a bad request, an unknown conversation
or a busy server is an ordinary JSON error. From step 4 on, a failure is an ``error`` frame (the
status is already 200) and ends the stream with no ``done``. The slot is held until the stream
ends: ``done``, ``error`` or the client leaving. The assistant message is stored once the whole
answer exists, before ``done`` is sent (its ``message_id`` is that message's id). An answer that
would reach 4,000 characters is cut at 3,999 plus an ellipsis, so what the client saw is exactly
what is stored. The whole generation has one deadline (the model's attempts plus the two retrieval
steps — about 4 minutes at the defaults), and a client that stops reading is dropped after 30 s
(``app.ai.streaming.SseResponse``): a slow provider or a stalled reader cannot hold a slot for ever.

Everything here is user content or model output: no question, answer, passage, title or id reaches a
log line, a span or a metric. Logs carry counts, booleans and a bounded error kind.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from langchain_core.messages import AnyMessage
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.citations import citation_of, cited_chunks, invalid_citation_numbers
from app.ai.config import get_ai_settings
from app.ai.graph import ask, build_graph
from app.ai.history import HISTORY_MESSAGES, history_messages
from app.ai.limits import NoFreeSlot, ask_slot, take_slot
from app.ai.streaming import iter_answer, sse, sse_response
from app.ai.telemetry import error_kind
from app.ai.tools.retrieve import RetrievedChunk
from app.db import get_session
from app.errors import AIUnavailable, ApiError, ErrorBody, NotFound, current_trace_id
from app.events import track_event
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
    await _preflight(conversation_id, question, session)

    try:
        with ask_slot(get_ai_settings().max_concurrent_asks):
            return await _ask(conversation_id, question, session, graph)
    except NoFreeSlot:
        raise ApiError("busy", status_code=429) from None


async def _preflight(conversation_id: UUID, question: str, session: AsyncSession) -> None:
    """The checks that cost nothing to fail: size (413), then existence (404)."""
    if len(question) > MAX_QUESTION_CHARS:
        raise ApiError("payload_too_large", status_code=413)
    if await repo.get_conversation(session, conversation_id) is None:
        raise NotFound()


async def _store_question(
    conversation_id: UUID, question: str, session: AsyncSession
) -> list[AnyMessage]:
    """Read the history, then store and commit the question; returns the history for the model.

    History is read first, so it never contains the new question; the question is committed before
    the model is called, so it survives any failure after this point."""
    history = history_messages(
        await repo.get_recent_messages(session, conversation_id, HISTORY_MESSAGES)
    )
    try:
        await repo.add_message(session, conversation_id, ROLE_USER, question)
    except repo.ConversationNotFound:
        raise NotFound() from None
    await session.commit()
    return history


async def _ask(conversation_id: UUID, question: str, session: AsyncSession, graph: Any) -> AskOut:
    history = await _store_question(conversation_id, question, session)

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


# ── the stream (api 4.3) ─────────────────────────────────────────────────────

_STREAM_TEXT_LIMIT = MAX_ANSWER_CHARS - len(_ELLIPSIS)

_STREAM_DESCRIPTION = (
    "Server-sent events. `sources` (`{sources: [{title, path}], count}`: every retrieved document, "
    "sent as soon as retrieval ends), then `token` (`{text}`) repeated, then `done` "
    "(`{message_id, input_tokens, output_tokens, citations}`: `citations` are the cited documents, "
    "exactly as stored) **or** `error` (`{error_kind, error, trace_id}`: the stream ends and no "
    "answer is stored). A bad request, an unknown conversation and a busy server are ordinary JSON "
    "errors before the stream starts."
)


def _document_citations(context: list[RetrievedChunk]) -> list[dict[str, str]]:
    """One ``{title, path}`` per retrieved document (a document can supply several passages).

    Documents are told apart exactly as ``cited_chunks`` does — by ``source``, and a passage with no
    source is its own document — so every document in ``done.citations`` is also in ``sources``."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for number, chunk in enumerate(context, start=1):
        key = chunk.get("source") or f"#{number}"
        if key not in seen:
            seen.add(key)
            out.append(citation_of(chunk))
    return out


def _wire_kind(kind: str) -> str:
    """The failure kind a client may see. An authentication failure says that this service's own
    identity is broken, which an anonymous caller has no use for: it goes out as plain ``error``."""
    return "error" if kind == "auth" else kind


def _int_or_none(value: Any) -> int | None:
    """A token count as a non-negative integer, or ``None`` (a provider's value is not trusted)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _record_failure(code: str, kind: str) -> None:
    """A stream that fails is still ``200``: this bounded event is how it shows up in telemetry."""
    track_event("ask.stream.failed", {"error": code, "error_kind": kind})


def _error_frame(code: str, kind: str) -> str:
    return sse("error", {"error_kind": kind, "error": code, "trace_id": current_trace_id()})


async def _close(events: Any) -> None:
    try:
        await events.aclose()
    except Exception:  # noqa: BLE001 - cleanup must not mask the real outcome
        pass


@router.post(
    "/{conversation_id}/ask/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": _STREAM_DESCRIPTION,
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        404: {"model": ErrorBody, "description": "No such conversation (`not_found`)"},
        413: {"model": ErrorBody, "description": "The question is too long (`payload_too_large`)"},
        429: {"model": ErrorBody, "description": "Too many asks at once (`busy`)"},
    },
    summary="Ask a question; the answer streams in",
)
async def ask_stream(
    conversation_id: UUID, body: AskIn, session: DbSession, graph: AnswerGraph
) -> StreamingResponse:
    question = body.question
    await _preflight(conversation_id, question, session)
    try:
        release = take_slot(get_ai_settings().max_concurrent_asks)
    except NoFreeSlot:
        raise ApiError("busy", status_code=429) from None
    try:
        history = await _store_question(conversation_id, question, session)
        # The response gives the slot back when it ends — however it ends (see SseResponse).
        return sse_response(
            _answer_frames(conversation_id, question, history, session, graph), on_close=release
        )
    except BaseException:
        release()  # idempotent: harmless if the response already holds it
        raise


async def _answer_frames(
    conversation_id: UUID,
    question: str,
    history: list[AnyMessage],
    session: AsyncSession,
    graph: Any,
) -> AsyncIterator[str]:
    """The SSE frames of one answer. Never raises: every failure becomes an ``error`` frame.

    Once the first byte is out, a raised exception cannot become a clean response, and the server
    span would record its text."""
    parts: list[str] = []
    length = 0
    was_cut = False
    context: list[RetrievedChunk] = []
    input_tokens: int | None = None
    output_tokens: int | None = None

    # One deadline for the whole generation: the model's attempts plus the two retrieval steps. The
    # client's own timeouts are per read, so a provider that trickles tokens would otherwise keep
    # the slot for as long as it likes.
    settings = get_ai_settings()
    budget = (
        settings.request_timeout_seconds * (settings.max_retries + 1)
        + 2 * settings.retrieval_timeout_seconds
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget

    events = iter_answer(graph, question, history=history)
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            try:
                async with asyncio.timeout(remaining):
                    event = await anext(events)
            except StopAsyncIteration:
                break
            if event.kind == "context":
                context = event.value
                yield sse(
                    "sources",
                    {"sources": _document_citations(context), "count": len(context)},
                )
            elif event.kind == "usage":
                i, o = event.value
                input_tokens = _int_or_none(i) if i is not None else input_tokens
                output_tokens = _int_or_none(o) if o is not None else output_tokens
            else:
                piece = clean_text(event.value)
                if not piece:
                    continue
                if length + len(piece) > _STREAM_TEXT_LIMIT:
                    piece = piece[: _STREAM_TEXT_LIMIT - length] + _ELLIPSIS
                    was_cut = True
                parts.append(piece)
                length += len(piece)
                yield sse("token", {"text": piece})
                if was_cut:
                    break
    except Exception as exc:  # noqa: BLE001 - the client gets a bounded kind, never the message
        kind = error_kind(exc)
        _log.warning("ask failed", kind=kind)
        _record_failure("ai_unavailable", kind)
        yield _error_frame("ai_unavailable", _wire_kind(kind))
        return
    finally:
        await _close(events)

    text = "".join(parts)
    if not text.strip():
        _log.warning("ask failed", kind="empty_answer")
        _record_failure("ai_unavailable", "empty_answer")
        yield _error_frame("ai_unavailable", "empty_answer")
        return

    cited = cited_chunks(text, context)
    citations = [citation_of(chunk) for chunk in cited]
    try:
        message = await repo.add_message(
            session,
            conversation_id,
            ROLE_ASSISTANT,
            text,
            citations=citations,
            token_count=output_tokens,
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - a database error's text can quote the data: kind only
        _log.error("stream persist failed", kind="persist_failed")
        _record_failure("internal_error", "persist_failed")
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        yield _error_frame("internal_error", "persist_failed")
        return

    invalid = len(invalid_citation_numbers(text, len(context)))
    _log.info("answer citations", retrieved=len(context), cited=len(cited), invalid=invalid)
    _log.info(
        "question answered",
        history=len(history),
        citations=len(citations),
        cut=was_cut,
        stream=True,
    )
    yield sse(
        "done",
        {
            "message_id": str(message.id),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "citations": citations,
        },
    )
