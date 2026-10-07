"""Streamed answers as Server-Sent Events (ADR-0009).

Wire format (one ``event:`` + one JSON ``data:`` line per event, blank-line terminated):

    event: sources   data: {"sources": ["docs/a.md"], "count": 3}   — after retrieval
    event: token     data: {"text": "partial "}                     — per model token chunk
    event: done      data: {"sources": [...], "input_tokens": 12, "output_tokens": 40}
    event: error     data: {"error_kind": "timeout"}                — then the stream ends

That is the template's stream (``stream_answer``). The Team Assistant's conversation stream
(``app/routers/ask.py``, ``POST /v1/conversations/{id}/ask/stream``) uses the same frame names with
richer payloads; both are built on ``iter_answer``, which turns the graph's stream into three kinds
of event — the retrieved context, token chunks and token usage — and leaves framing to the caller.

Use from a ``/v1`` route::

    @router.post("/chat/stream")
    async def chat_stream(body: ChatIn) -> StreamingResponse:
        graph = build_graph()
        return sse_response(stream_answer(graph, body.question))

The BFF forwards it with the streaming pass-through helper (``apps/web/src/lib/stream.ts``),
not ``fetchUpstream`` (which forces a JSON accept header and a 10 s timeout).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, NamedTuple

from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk, AnyMessage, HumanMessage
from starlette.types import Message, Receive, Scope, Send

from app.ai.graph import GraphState, _message_text, unique_sources
from app.ai.telemetry import error_kind, extract_usage

SSE_MEDIA_TYPE = "text/event-stream"

# A client that accepts no bytes for this long is dropped: the response ends, its slot is freed.
STREAM_WRITE_TIMEOUT_SECONDS = 30.0

# ``json.dumps(ensure_ascii=False)`` escapes CR, LF and the other control characters below U+0020,
# which are the only line breaks the SSE specification knows. It leaves these four raw. They are not
# SSE line breaks, but a parser that splits lines with ``str.splitlines()`` (or its JavaScript
# equivalent) treats three of them as such, and model output can contain them. Escaped, a frame is
# one physical line to every parser.
_RAW_LINE_BREAKS = ("\u2028", "\u2029", "\u0085", "\u007f")


def sse(event: str, data: dict[str, Any]) -> str:
    """Format one SSE frame: one ``event:`` line, one ``data:`` line, a blank line."""
    body = json.dumps(data, ensure_ascii=False)
    for char in _RAW_LINE_BREAKS:
        body = body.replace(char, f"\\u{ord(char):04x}")
    return f"event: {event}\ndata: {body}\n\n"


class AnswerEvent(NamedTuple):
    """One thing the graph produced while answering.

    ``kind`` is ``"context"`` (``value``: the retrieved chunks, once, before any token),
    ``"token"`` (``value``: a piece of the answer text) or ``"usage"`` (``value``: an
    ``(input_tokens, output_tokens)`` pair, either of which may be ``None``).
    """

    kind: str
    value: Any


async def iter_answer(
    graph: Any,
    question: str,
    *,
    history: list[AnyMessage] | None = None,
    config: dict[str, Any] | None = None,
) -> AsyncIterator[AnswerEvent]:
    """Run the graph for ``question`` and yield its events as they happen.

    Failures (retrieval, the model, a missing configuration) are raised, not framed: the caller
    decides what the client sees. Close the iterator (``aclose()``) if you stop early.
    """
    inputs: GraphState = {
        "messages": [*(history or []), HumanMessage(content=question)],
        "context": [],
    }
    stream = graph.astream(inputs, config, stream_mode=["updates", "messages"])
    try:
        async for mode, payload in stream:
            if mode == "updates":
                for node, update in (payload or {}).items():
                    if node == "retrieve":
                        yield AnswerEvent("context", (update or {}).get("context", []) or [])
                    elif node == "answer":
                        for message in (update or {}).get("messages", []) or []:
                            yield AnswerEvent("usage", extract_usage(message))
            elif mode == "messages":
                chunk, metadata = payload
                if (
                    isinstance(chunk, AIMessageChunk)
                    and (metadata or {}).get("langgraph_node") == "answer"
                    and not getattr(chunk, "tool_call_chunks", None)
                ):
                    text = _message_text(chunk)
                    if text:
                        yield AnswerEvent("token", text)
    finally:
        # Closing this iterator does not close the graph's stream underneath it: do it here, so
        # stopping early (an answer cut at the limit, a client that left) ends the model call now
        # instead of whenever the garbage collector gets to it.
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - cleanup must not mask the real outcome
                pass


async def stream_answer(
    graph: Any,
    question: str,
    *,
    history: list[AnyMessage] | None = None,
    config: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Run the graph and yield SSE frames: ``sources`` → ``token``* → ``done`` (or ``error``)."""
    sources: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    events = iter_answer(graph, question, history=history, config=config)
    try:
        async for event in events:
            if event.kind == "context":
                sources = unique_sources(event.value)
                yield sse("sources", {"sources": sources, "count": len(event.value)})
            elif event.kind == "usage":
                i, o = event.value
                input_tokens = i if i is not None else input_tokens
                output_tokens = o if o is not None else output_tokens
            else:
                yield sse("token", {"text": event.value})
    except Exception as exc:  # noqa: BLE001 - the frame carries a bounded kind, never the message
        yield sse("error", {"error_kind": error_kind(exc)})
        return
    finally:
        try:
            await events.aclose()
        except Exception:  # noqa: BLE001 - cleanup must not mask the real outcome
            pass
    yield sse(
        "done",
        {"sources": sources, "input_tokens": input_tokens, "output_tokens": output_tokens},
    )


class SseResponse(StreamingResponse):
    """An SSE response that runs ``on_close`` exactly once when it ends, however it ends.

    "Ends" means the stream finished, the generator raised, the client went away and the response
    was cancelled, or a write timed out. ``on_close`` runs in ``__call__``'s ``finally`` — not in
    the generator's — so it does not depend on when an abandoned generator is garbage-collected, and
    it runs even if closing the generator is itself cancelled. The frame generator is closed there
    too, so its own cleanup runs promptly.

    Every write is bounded by ``write_timeout`` seconds: a client that stops reading cannot hold the
    response (and the concurrency slot ``on_close`` gives back) open for ever. The timeout raises
    ``TimeoutError`` out of the response, which ends the connection.
    """

    def __init__(
        self,
        frames: AsyncIterator[str],
        *,
        on_close: Callable[[], None] | None = None,
        write_timeout: float | None = STREAM_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            frames,
            media_type=SSE_MEDIA_TYPE,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self._on_close = on_close
        self._write_timeout = write_timeout

    async def stream_response(self, send: Send) -> None:
        if self._write_timeout is None:
            await super().stream_response(send)
            return
        timeout = self._write_timeout

        async def bounded(message: Message) -> None:
            async with asyncio.timeout(timeout):
                await send(message)

        await super().stream_response(bounded)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                aclose = getattr(self.body_iterator, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # noqa: BLE001 - cleanup must not mask the real outcome
                        pass
            finally:
                # In its own `finally`: a cancelled close above must not skip giving the slot back.
                on_close, self._on_close = self._on_close, None
                if on_close is not None:
                    on_close()


def sse_response(
    frames: AsyncIterator[str],
    *,
    on_close: Callable[[], None] | None = None,
    write_timeout: float | None = STREAM_WRITE_TIMEOUT_SECONDS,
) -> StreamingResponse:
    """A FastAPI response that streams SSE frames without proxy buffering."""
    return SseResponse(frames, on_close=on_close, write_timeout=write_timeout)
