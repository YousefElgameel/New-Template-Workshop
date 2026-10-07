# ruff: noqa: F811 - pytest fixtures imported from sibling test modules and used as parameters
"""Hardening of ``POST /v1/conversations/{id}/ask/stream`` (api 4.3 reviews).

Frame escaping, the overall deadline and the write timeout, slot release under a cancelled scope,
which failure kinds reach the wire, token counts, the failure event, the cut boundary, and what the
OpenTelemetry instrumentation records for a stream. Offline: fakes only (rule 40).

Characters that are invisible or that editors rewrite are built with ``chr()``, never typed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
import structlog.testing
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.requests import ClientDisconnect

import app.ai.limits as limits_mod
import app.observability as obs
import app.routers.ask as ask_mod
from app.ai.streaming import STREAM_WRITE_TIMEOUT_SECONDS, SseResponse, sse, stream_answer
from app.db import get_session
from app.routers.ask import MAX_ANSWER_CHARS, get_answer_graph
from tests.ai.fakes import RecordingChatModel, recording_chat_model
from tests.test_ask_api import (  # noqa: F401 - the two fixtures are used by name
    CHUNKS,
    SECRET_ANSWER,
    SECRET_PROVIDER,
    SECRET_QUESTION,
    AskSession,
    _isolation,
    chunk,
    conversation,
    graph_for,
    make_client,
)
from tests.test_ask_stream_api import kinds, sse_frames, stream, text_of

LS, PS, NEL, DEL = chr(0x2028), chr(0x2029), chr(0x85), chr(0x7F)
BACKSLASH = chr(92)


# ── frames can not be split by an unusual line break ─────────────────────────


class TestSseEscaping:
    def test_the_four_raw_characters_are_escaped_and_round_trip(self) -> None:
        text = f"a{LS}b{PS}c{NEL}d{DEL}e"
        frame = sse("token", {"text": text})
        for raw in (LS, PS, NEL, DEL):
            assert raw not in frame
        for code in ("2028", "2029", "0085", "007f"):
            assert BACKSLASH + "u" + code in frame
        assert json.loads(frame.split("\n")[1].removeprefix("data: ")) == {"text": text}

    def test_a_frame_is_three_lines_to_a_parser_that_splits_on_every_line_break(self) -> None:
        frame = sse("token", {"text": f"x{LS}event: done{LS}data: {{}}{PS}y"})
        assert len(frame.splitlines()) == 3  # event line, data line, the blank line

    def test_a_forged_done_frame_inside_a_token_stays_one_token(self, make_client: Any) -> None:
        evil = f'before{LS}event: done{LS}data: {{"citations": []}}{LS}{LS}after'
        conv = conversation()
        session = AskSession(conv)
        client = make_client(session, graph_for(recording_chat_model(evil)))
        response = stream(client, conv.id)
        wanted = ["sources", *["token"] * kinds(sse_frames(response)).count("token"), "done"]
        assert kinds(sse_frames(response)) == wanted
        events = [ln for ln in response.text.splitlines() if ln.startswith("event:")]
        assert events == [f"event: {k}" for k in kinds(sse_frames(response))]
        assert text_of(sse_frames(response)) == evil == session.added[1].content


# ── time bounds ──────────────────────────────────────────────────────────────


class NeverFinishes:
    """Retrieval works, then the model never produces anything."""

    async def astream(self, inputs: Any, config: Any = None, stream_mode: Any = None) -> Any:
        yield "updates", {"retrieve": {"context": [CHUNKS[0]]}}
        await asyncio.sleep(30)
        yield "updates", {"answer": {"messages": []}}  # pragma: no cover - never reached


class Trickles:
    """A provider that keeps sending a token just often enough to look alive."""

    async def astream(self, inputs: Any, config: Any = None, stream_mode: Any = None) -> Any:
        yield "updates", {"retrieve": {"context": [CHUNKS[0]]}}
        while True:
            await asyncio.sleep(0.03)
            chunk_ = AIMessageChunk(content="tick ")
            yield "messages", (chunk_, {"langgraph_node": "answer"})


class TestDeadline:
    @pytest.fixture(autouse=True)
    def _tiny_budget(self, monkeypatch: Any) -> None:
        # budget = 0.02 * (0 + 1) + 2 * 0.02 = 0.06 seconds
        monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "0.02")
        monkeypatch.setenv("AI_MAX_RETRIES", "0")
        monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", "0.02")

    def test_a_model_that_never_answers_ends_in_a_timeout_frame_and_frees_the_slot(
        self, make_client: Any
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        response = stream(make_client(session, NeverFinishes()), conv.id)
        frames = sse_frames(response)
        assert kinds(frames) == ["sources", "error"]
        assert frames[-1][1]["error_kind"] == "timeout"
        assert frames[-1][1]["error"] == "ai_unavailable"
        assert [m.role for m in session.added] == ["user"]
        assert limits_mod.slots_in_use() == 0

    def test_a_provider_that_trickles_tokens_does_not_outlast_the_deadline(
        self, make_client: Any
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        response = stream(make_client(session, Trickles()), conv.id)
        frames = sse_frames(response)
        assert kinds(frames)[-1] == "error" and frames[-1][1]["error_kind"] == "timeout"
        assert "token" in kinds(frames)  # it did stream for a while
        assert [m.role for m in session.added] == ["user"]
        assert limits_mod.slots_in_use() == 0


class TestWriteTimeout:
    @pytest.mark.parametrize("spec_version", ["2.3", "2.4"])  # uvicorn reports 2.3
    def test_a_client_that_stops_reading_ends_the_response_and_frees_the_slot(
        self, spec_version: str
    ) -> None:
        released: list[int] = []
        closed: list[int] = []

        async def frames() -> Any:
            try:
                yield "event: sources\ndata: {}\n\n"
                yield "event: done\ndata: {}\n\n"
            finally:
                closed.append(1)

        async def scenario() -> None:
            response = SseResponse(
                frames(), on_close=lambda: released.append(1), write_timeout=0.05
            )

            async def send(message: Any) -> None:
                if message.get("body"):
                    await asyncio.sleep(30)  # the socket accepts no more bytes

            async def receive() -> Any:
                await asyncio.sleep(30)
                return {"type": "http.disconnect"}

            scope = {"type": "http", "asgi": {"spec_version": spec_version}}
            # TimeoutError is an OSError, which Starlette turns into ClientDisconnect on spec 2.4
            with pytest.raises((TimeoutError, ClientDisconnect)):
                await response(scope, receive, send)

        asyncio.run(asyncio.wait_for(scenario(), timeout=10))
        assert released == [1] and closed == [1]

    def test_the_default_is_thirty_seconds_and_none_disables_it(self) -> None:
        assert STREAM_WRITE_TIMEOUT_SECONDS == 30.0
        sent: list[Any] = []

        async def frames() -> Any:
            yield "event: done\ndata: {}\n\n"

        async def scenario() -> None:
            response = SseResponse(frames(), write_timeout=None)

            async def send(message: Any) -> None:
                sent.append(message)

            async def receive() -> Any:
                await asyncio.sleep(30)
                return {"type": "http.disconnect"}

            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

        asyncio.run(asyncio.wait_for(scenario(), timeout=10))
        assert [m["type"] for m in sent] == [
            "http.response.start",
            "http.response.body",
            "http.response.body",
        ]


class TestSlotRelease:
    def test_the_slot_is_given_back_even_when_closing_the_generator_is_cancelled(self) -> None:
        """A response running under a cancelled anyio scope: the generator is suspended at a yield
        whose cleanup awaits, and that await is cancelled. ``on_close`` must still run."""
        released: list[int] = []

        async def frames() -> Any:
            try:
                yield "event: sources\ndata: {}\n\n"
                await asyncio.sleep(30)
            finally:
                await asyncio.sleep(0)  # a real await in the cleanup, like closing the model stream

        async def scenario() -> None:
            response = SseResponse(
                frames(), on_close=lambda: released.append(1), write_timeout=None
            )
            with anyio.CancelScope() as scope:

                async def send(message: Any) -> None:
                    if message.get("body"):
                        scope.cancel()
                        await asyncio.sleep(0)

                async def receive() -> Any:
                    await asyncio.sleep(30)
                    return {"type": "http.disconnect"}

                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

        asyncio.run(asyncio.wait_for(scenario(), timeout=10))
        assert released == [1]

    def test_building_the_response_failing_does_not_leak_the_slot(
        self, make_client: Any, monkeypatch: Any
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        client = make_client(session, graph_for(recording_chat_model("x")))

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("cannot build the response")

        monkeypatch.setattr(ask_mod, "sse_response", boom)
        assert stream(client, conv.id).status_code == 500
        assert limits_mod.slots_in_use() == 0


# ── what a client may learn from an error frame ──────────────────────────────


def raising(exc: Exception) -> RecordingChatModel:
    class Raising(RecordingChatModel):
        def _stream(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
            raise exc
            yield  # pragma: no cover - makes this a generator

    return Raising(messages=iter([]))


class AuthenticationError(Exception): ...


class RateLimitError(Exception): ...


class TestErrorKinds:
    def _kind(self, make_client: Any, exc: Exception, monkeypatch: Any) -> tuple[str, dict]:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            ask_mod, "track_event", lambda name, attrs=None: events.append((name, attrs or {}))
        )
        conv = conversation()
        client = make_client(AskSession(conv), graph_for(raising(exc)))
        frames = sse_frames(stream(client, conv.id))
        assert frames[-1][0] == "error"
        (event,) = events
        assert event[0] == "ask.stream.failed"
        return frames[-1][1]["error_kind"], event[1]

    def test_an_authentication_failure_is_a_plain_error_on_the_wire_but_exact_in_telemetry(
        self, make_client: Any, monkeypatch: Any
    ) -> None:
        wire, event = self._kind(make_client, AuthenticationError("bad key"), monkeypatch)
        assert wire == "error"
        assert event == {"error": "ai_unavailable", "error_kind": "auth"}

    def test_a_rate_limit_reaches_the_client_so_it_can_try_later(
        self, make_client: Any, monkeypatch: Any
    ) -> None:
        wire, event = self._kind(make_client, RateLimitError("slow down"), monkeypatch)
        assert wire == "rate_limited" and event["error_kind"] == "rate_limited"

    def test_a_timeout_and_a_connection_failure(self, make_client: Any, monkeypatch: Any) -> None:
        assert self._kind(make_client, TimeoutError("x"), monkeypatch)[0] == "timeout"
        assert self._kind(make_client, ConnectionError("x"), monkeypatch)[0] == "network"

    def test_the_failure_event_is_bounded_and_content_free(
        self, make_client: Any, monkeypatch: Any
    ) -> None:
        _, event = self._kind(
            make_client, RuntimeError(f"{SECRET_PROVIDER} {SECRET_QUESTION}"), monkeypatch
        )
        assert set(event) == {"error", "error_kind"}
        assert SECRET_PROVIDER not in repr(event) and SECRET_QUESTION not in repr(event)

    def test_a_blank_answer_and_a_persistence_failure_are_recorded_too(
        self, make_client: Any, monkeypatch: Any
    ) -> None:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            ask_mod, "track_event", lambda name, attrs=None: events.append((name, attrs or {}))
        )
        conv = conversation()
        stream(make_client(AskSession(conv), graph_for(recording_chat_model("  "))), conv.id)

        class Session(AskSession):
            async def commit(self) -> None:
                if self.commits >= 1:
                    raise RuntimeError(SECRET_PROVIDER)
                await super().commit()

        stream(make_client(Session(conv), graph_for(recording_chat_model("fine [1]"))), conv.id)
        assert [e[1] for e in events] == [
            {"error": "ai_unavailable", "error_kind": "empty_answer"},
            {"error": "internal_error", "error_kind": "persist_failed"},
        ]

    def test_a_good_stream_records_no_failure(self, make_client: Any, monkeypatch: Any) -> None:
        events: list[Any] = []
        monkeypatch.setattr(ask_mod, "track_event", lambda *a, **k: events.append(a))
        conv = conversation()
        stream(make_client(AskSession(conv), graph_for(recording_chat_model("ok [1]"))), conv.id)
        assert events == []


# ── token counts are numbers the client can trust ────────────────────────────


class UsageGraph:
    def __init__(self, usage: dict[str, Any]) -> None:
        self.usage = usage

    async def astream(self, inputs: Any, config: Any = None, stream_mode: Any = None) -> Any:
        yield "updates", {"retrieve": {"context": [CHUNKS[0]]}}
        yield "messages", (AIMessageChunk(content="hello [1]"), {"langgraph_node": "answer"})
        # A plain object, not an AIMessage: AIMessage validates (and coerces) its usage, so an
        # invalid count could never get through it. This is the route's own guard under test.
        message = SimpleNamespace(content="hello [1]", usage_metadata=self.usage)
        yield "updates", {"answer": {"messages": [message]}}


class TestTokenCounts:
    @pytest.mark.parametrize(
        ("usage", "expected"),
        [
            ({"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}, (12, 7)),
            ({"input_tokens": -1, "output_tokens": 7, "total_tokens": 6}, (None, 7)),
            ({"input_tokens": 12, "output_tokens": -5, "total_tokens": 7}, (12, None)),
            ({"input_tokens": 3.5, "output_tokens": 7, "total_tokens": 10}, (None, 7)),
            ({"input_tokens": True, "output_tokens": "7", "total_tokens": 0}, (None, None)),
        ],
    )
    def test_only_non_negative_integers_reach_the_client_and_the_database(
        self, make_client: Any, usage: dict, expected: tuple[int | None, int | None]
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        frames = sse_frames(stream(make_client(session, UsageGraph(usage)), conv.id))
        done = frames[-1][1]
        assert (done["input_tokens"], done["output_tokens"]) == expected
        assert session.added[1].token_count == expected[1]


# ── the cut, at its boundary ─────────────────────────────────────────────────


class TestCutBoundary:
    def _stream_of(self, make_client: Any, n: int) -> tuple[str, str, AskSession]:
        conv = conversation()
        session = AskSession(conv)
        model = recording_chat_model("x" * n)  # one token: no spaces to split on
        frames = sse_frames(stream(make_client(session, graph_for(model)), conv.id))
        assert kinds(frames)[-1] == "done"
        return text_of(frames), session.added[1].content, session

    def test_just_below_the_limit_is_kept_whole(self, make_client: Any) -> None:
        streamed, stored, _ = self._stream_of(make_client, MAX_ANSWER_CHARS - 1)
        assert streamed == stored == "x" * (MAX_ANSWER_CHARS - 1)

    @pytest.mark.parametrize("n", [MAX_ANSWER_CHARS, MAX_ANSWER_CHARS + 1, MAX_ANSWER_CHARS * 3])
    def test_an_answer_that_reaches_the_limit_is_cut_to_the_limit_with_an_ellipsis(
        self, make_client: Any, n: int
    ) -> None:
        streamed, stored, _ = self._stream_of(make_client, n)
        assert streamed == stored
        assert len(stored) == MAX_ANSWER_CHARS and stored.endswith("…")
        assert stored[:-1] == "x" * (MAX_ANSWER_CHARS - 1)


class ImmediateGraph:
    """Every event is available at once: this async generator never suspends."""

    async def astream(self, inputs: Any, config: Any = None, stream_mode: Any = None) -> Any:
        yield "updates", {"retrieve": {"context": [CHUNKS[0]]}}
        for _ in range(50):
            yield "messages", (AIMessageChunk(content="x"), {"langgraph_node": "answer"})


class TestDeadlineIsChecked:
    def test_a_spent_budget_stops_a_stream_whose_events_never_wait(
        self, make_client: Any, monkeypatch: Any
    ) -> None:
        """``asyncio.timeout`` only acts when the awaited call suspends; events that arrive without
        suspending would sail through it. The explicit check before each step closes that."""
        monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "1e-9")
        monkeypatch.setenv("AI_MAX_RETRIES", "0")
        monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", "1e-9")
        conv = conversation()
        session = AskSession(conv)
        frames = sse_frames(stream(make_client(session, ImmediateGraph()), conv.id))
        assert kinds(frames) == ["error"] and frames[0][1]["error_kind"] == "timeout"
        assert [m.role for m in session.added] == ["user"]


class TestTemplateStreamCloses:
    def test_closing_the_template_stream_closes_the_graph_stream_under_it(self) -> None:
        closed: list[int] = []

        class Graph:
            async def astream(
                self, inputs: Any, config: Any = None, stream_mode: Any = None
            ) -> Any:
                try:
                    yield "updates", {"retrieve": {"context": [CHUNKS[0]]}}
                    for _ in range(100):
                        yield (
                            "messages",
                            (AIMessageChunk(content="x"), {"langgraph_node": "answer"}),
                        )
                finally:
                    closed.append(1)

        async def scenario() -> list[int]:
            frames = stream_answer(Graph(), "q")
            await frames.__anext__()  # the `sources` frame
            await frames.aclose()  # the consumer stops reading
            return list(closed)

        assert asyncio.run(asyncio.wait_for(scenario(), timeout=10)) == [1]


# ── sources and citations name documents the same way ────────────────────────


class TestDocumentCitations:
    def test_passages_without_a_source_are_separate_documents_in_both_places(
        self, make_client: Any
    ) -> None:
        chunks = [chunk(1, source=""), chunk(2, source=""), chunk(3, "docs/c.md", "C")]
        conv = conversation()
        client = make_client(
            AskSession(conv), graph_for(recording_chat_model("one [1] two [2] three [3]"), chunks)
        )
        frames = sse_frames(stream(client, conv.id))
        listed = frames[0][1]["sources"]
        cited = frames[-1][1]["citations"]
        assert len(listed) == 3 and len(cited) == 3
        assert all(c in listed for c in cited)  # every cited document was announced first


# ── what the instrumentation records ─────────────────────────────────────────


def _instrumented(exclude: list[str] | None = None) -> tuple[FastAPI, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    api = FastAPI()
    api.include_router(ask_mod.router, prefix="/v1")
    kwargs: dict[str, Any] = {"tracer_provider": provider}
    if exclude is not None:
        kwargs["exclude_spans"] = exclude
    FastAPIInstrumentor.instrument_app(api, **kwargs)
    return api, exporter


def _everything(exporter: InMemorySpanExporter) -> str:
    parts = []
    for span in exporter.get_finished_spans():
        parts.append(f"{span.name} {dict(span.attributes or {})}")
        parts.extend(f"{e.name} {dict(e.attributes or {})}" for e in span.events)
        parts.append(f"{span.status.status_code} {span.status.description}")
    return " | ".join(parts)


class TestSpans:
    def _run(self, api: FastAPI, session: Any, graph: Any, conv_id: Any, raising: bool) -> None:
        async def override() -> Any:
            yield session

        api.dependency_overrides[get_session] = override
        api.dependency_overrides[get_answer_graph] = lambda: graph
        try:
            client = TestClient(api, raise_server_exceptions=raising)
            client.post(f"/v1/conversations/{conv_id}/ask/stream", json={"question": "q"})
        finally:
            FastAPIInstrumentor.uninstrument_app(api)

    def test_a_database_failure_while_storing_the_answer_puts_no_driver_text_on_any_span(
        self,
    ) -> None:
        conv = conversation()

        class Session(AskSession):
            async def commit(self) -> None:
                if self.commits >= 1:
                    raise RuntimeError(f"driver said: {SECRET_PROVIDER}")
                await super().commit()

        api, exporter = _instrumented()
        self._run(api, Session(conv), graph_for(recording_chat_model("fine [1]")), conv.id, True)
        text = _everything(exporter)
        assert exporter.get_finished_spans(), "the request produced no span"
        assert SECRET_PROVIDER not in text
        assert all(not span.events for span in exporter.get_finished_spans())  # no exception event

    def test_control_an_exception_after_the_first_byte_does_put_its_text_on_the_span(self) -> None:
        """Why the stream never raises: this is what the instrumentation does with an exception."""
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        api = FastAPI()

        @api.get("/boom")
        async def boom() -> StreamingResponse:
            async def frames() -> Any:
                yield "event: sources\ndata: {}\n\n"
                raise RuntimeError(f"driver said: {SECRET_PROVIDER}")

            return StreamingResponse(frames(), media_type="text/event-stream")

        FastAPIInstrumentor.instrument_app(api, tracer_provider=provider)
        try:
            TestClient(api, raise_server_exceptions=False).get("/boom")
        finally:
            FastAPIInstrumentor.uninstrument_app(api)
        assert SECRET_PROVIDER in _everything(exporter)

    def test_a_stream_makes_one_span_not_one_per_frame(self) -> None:
        conv = conversation()

        def spans_for(exclude: list[str] | None) -> list[str]:
            api, exporter = _instrumented(exclude)
            self._run(
                api,
                AskSession(conv),
                graph_for(recording_chat_model("one two three four five six seven [1]")),
                conv.id,
                True,
            )
            return [s.name for s in exporter.get_finished_spans()]

        without = spans_for(None)  # the library default: a span for every send and receive
        with_exclusion = spans_for(["send", "receive"])
        assert sum(name.endswith("http send") for name in without) >= 8  # a frame is a send
        assert not any(name.endswith(("http send", "http receive")) for name in with_exclusion)
        assert len(with_exclusion) <= 2

    def test_the_service_turns_those_spans_off(self, monkeypatch: Any) -> None:
        seen: dict[str, Any] = {}

        def fake_instrument_app(app: Any, **kwargs: Any) -> None:
            seen.update(kwargs)

        class _Log:
            def warning(self, *a: Any, **k: Any) -> None: ...

        monkeypatch.setattr(
            FastAPIInstrumentor, "instrument_app", staticmethod(fake_instrument_app)
        )
        obs._instrument_fastapi(object(), _Log())
        assert seen.get("exclude_spans") == ["send", "receive"]


# ── logs ─────────────────────────────────────────────────────────────────────


def test_a_timeout_failure_logs_a_kind_only(make_client: Any, monkeypatch: Any) -> None:
    for name, value in (
        ("AI_REQUEST_TIMEOUT_SECONDS", "0.02"),
        ("AI_MAX_RETRIES", "0"),
        ("AI_RETRIEVAL_TIMEOUT_SECONDS", "0.02"),
    ):
        monkeypatch.setenv(name, value)
    conv = conversation()
    client = make_client(AskSession(conv), NeverFinishes())
    with structlog.testing.capture_logs() as logs:
        stream(client, conv.id, SECRET_QUESTION)
    rendered = repr(logs)
    assert "ask failed" in rendered and SECRET_QUESTION not in rendered
    assert SECRET_ANSWER not in rendered
