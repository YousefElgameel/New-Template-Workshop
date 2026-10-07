# ruff: noqa: F811 - pytest fixtures imported from test_ask_api and used as parameters
"""``POST /v1/conversations/{id}/ask/stream`` (roadmap api 4.3).

Same arrangement as ``test_ask_api.py``: the real app, with the session and the AI graph replaced by
fakes — no database, no model, no search, no network. The repository, ``app.ai.graph`` and the
streaming code run for real, so the frames, what is stored and what is logged are asserted on the
genuine code paths.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
import structlog.testing
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

import app.ai.limits as limits_mod
import app.routers.ask as ask_mod
from app.ai.streaming import SseResponse
from app.db import get_session
from app.main import app
from app.routers.ask import MAX_ANSWER_CHARS, MAX_QUESTION_CHARS, get_answer_graph
from tests.ai.fakes import FakeRetriever, RecordingChatModel, recording_chat_model
from tests.test_ask_api import (  # noqa: F401 - the two fixtures are used by name
    CHUNKS,
    INBOUND,
    SECRET_ANSWER,
    SECRET_PASSAGE,
    SECRET_PROVIDER,
    SECRET_QUESTION,
    TRACE_RE,
    AskSession,
    BoomModel,
    BoomRetriever,
    _isolation,
    assert_error,
    conversation,
    graph_for,
    make_client,
)


def sse_frames(response: Any) -> list[tuple[str, dict]]:
    """The ``(event, data)`` frames of a finished SSE response body."""
    out: list[tuple[str, dict]] = []
    for block in response.text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return out


def stream(
    client: TestClient, conversation_id: Any, question: Any = "What is the port?", **kw: Any
):
    return client.post(
        f"/v1/conversations/{conversation_id}/ask/stream", json={"question": question}, **kw
    )


def kinds(frames: list[tuple[str, dict]]) -> list[str]:
    return [event for event, _ in frames]


def text_of(frames: list[tuple[str, dict]]) -> str:
    return "".join(data["text"] for event, data in frames if event == "token")


# ── the happy path ───────────────────────────────────────────────────────────


class TestFrames:
    def test_sources_then_tokens_then_done(self, make_client: Any) -> None:
        conv = conversation()
        session = AskSession(conv)
        model = recording_chat_model("The port is 8000 [2], and 3000 for the web app [3].")
        client = make_client(session, graph_for(model))
        response = stream(client, conv.id, headers={"x-trace-id": INBOUND})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-trace-id"] == INBOUND
        assert response.headers["cache-control"] == "no-cache"

        frames = sse_frames(response)
        assert kinds(frames)[0] == "sources" and kinds(frames)[-1] == "done"
        assert set(kinds(frames)) == {"sources", "token", "done"}
        assert kinds(frames).count("sources") == 1 and kinds(frames).count("done") == 1
        assert kinds(frames).index("sources") < kinds(frames).index("token")

        _, sources = frames[0]
        assert set(sources) == {"sources", "count"} and sources["count"] == 3
        assert sources["sources"] == [
            {"title": "First doc", "path": "docs/doc1.md"},
            {"title": "Second doc", "path": "docs/doc2.md"},
            {"title": "Third doc", "path": "docs/doc3.md"},
        ]
        assert text_of(frames) == "The port is 8000 [2], and 3000 for the web app [3]."
        assert all(set(d) == {"text"} for e, d in frames if e == "token")

        _, done = frames[-1]
        assert set(done) == {"message_id", "input_tokens", "output_tokens", "citations"}
        assert done["citations"] == [
            {"title": "Second doc", "path": "docs/doc2.md"},
            {"title": "Third doc", "path": "docs/doc3.md"},
        ]
        assert done["input_tokens"] is None or isinstance(done["input_tokens"], int)

    def test_the_stored_answer_is_exactly_what_was_streamed(self, make_client: Any) -> None:
        conv = conversation()
        session = AskSession(conv)
        client = make_client(
            session, graph_for(recording_chat_model("Answer one [1] and two [3]."))
        )
        frames = sse_frames(stream(client, conv.id, "  Which?  "))
        user, assistant = session.added
        assert (user.role, user.content) == ("user", "Which?")
        assert assistant.role == "assistant" and assistant.content == text_of(frames)
        done = frames[-1][1]
        assert str(assistant.id) == done["message_id"]
        assert (
            assistant.citations
            == done["citations"]
            == [
                {"title": "First doc", "path": "docs/doc1.md"},
                {"title": "Third doc", "path": "docs/doc3.md"},
            ]
        )
        assert assistant.token_count == done["output_tokens"]

    def test_the_sources_frame_lists_every_document_once_even_when_none_is_cited(
        self, make_client: Any
    ) -> None:
        chunks = [
            {**CHUNKS[0]},
            {**CHUNKS[0], "id": "9", "content": "a second passage of the same document"},
            {**CHUNKS[1]},
        ]
        conv = conversation()
        session = AskSession(conv)
        client = make_client(session, graph_for(recording_chat_model("I do not know."), chunks))
        frames = sse_frames(stream(client, conv.id))
        assert frames[0][1]["count"] == 3  # passages retrieved
        assert [s["path"] for s in frames[0][1]["sources"]] == ["docs/doc1.md", "docs/doc2.md"]
        assert frames[-1][1]["citations"] == []  # …but the answer cites none
        assert session.added[1].citations == []

    def test_done_citations_are_the_ones_the_json_route_returns(self, make_client: Any) -> None:
        text = "Yes [2]. Also [1] and [9]."
        conv = conversation()
        client = make_client(AskSession(conv), graph_for(recording_chat_model(text, text)))
        streamed = sse_frames(stream(client, conv.id))[-1][1]["citations"]
        json_route = client.post(
            f"/v1/conversations/{conv.id}/ask", json={"question": "again"}
        ).json()["citations"]
        assert (
            streamed
            == json_route
            == [
                {"title": "Second doc", "path": "docs/doc2.md"},
                {"title": "First doc", "path": "docs/doc1.md"},
            ]
        )

    def test_control_characters_are_cleaned_before_they_are_streamed_and_stored(
        self, make_client: Any
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        client = make_client(session, graph_for(recording_chat_model("ok\x01x \x00y [1]")))
        frames = sse_frames(stream(client, conv.id))
        assert text_of(frames) == "ok x y [1]" == session.added[1].content
        assert "\x01" not in json.dumps(frames) and "\\u0001" not in json.dumps(frames)


# ── what is stored, and when ─────────────────────────────────────────────────


class TestOrder:
    def test_the_question_is_committed_before_the_model_and_the_answer_only_at_the_end(
        self, make_client: Any
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        seen: dict[str, Any] = {}

        class Spy(RecordingChatModel):
            def _stream(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
                seen["events_at_model_call"] = list(session.events)
                yield from super()._stream(messages, *args, **kwargs)

        model = Spy(messages=iter([AIMessage(content="one two three [1]")]))
        client = make_client(session, graph_for(model))
        assert stream(client, conv.id, "How?").status_code == 200
        assert seen["events_at_model_call"] == ["history", "add:user", "commit"]
        assert session.events == ["history", "add:user", "commit", "add:assistant", "commit"]

    def test_no_answer_is_stored_while_tokens_are_still_coming(self) -> None:
        conv = conversation()
        session = AskSession(conv)
        graph = graph_for(recording_chat_model("alpha beta gamma delta [1]"))

        async def scenario() -> list[list[str]]:
            history = await ask_mod._store_question(conv.id, "q", session)
            snapshots: list[list[str]] = []
            frames = ask_mod._answer_frames(conv.id, "q", history, session, graph)
            async for frame in frames:
                snapshots.append(list(session.events))
                if frame.startswith("event: done"):
                    break
            return snapshots

        snapshots = asyncio.run(scenario())
        assert all("add:assistant" not in s for s in snapshots[:-1])  # every frame before `done`
        assert snapshots[-1][-2:] == ["add:assistant", "commit"]  # stored just before `done`

    def test_the_database_session_stays_open_while_the_response_streams(
        self, make_client: Any
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        state = {"closed": False, "closed_during_stream": None}

        async def override() -> Any:
            try:
                yield session
            finally:
                state["closed"] = True

        class Spy(RecordingChatModel):
            def _stream(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
                state["closed_during_stream"] = state["closed"]
                yield from super()._stream(messages, *args, **kwargs)

        client = make_client(session, graph_for(Spy(messages=iter([AIMessage(content="ok [1]")]))))
        app.dependency_overrides[get_session] = override
        assert kinds(sse_frames(stream(client, conv.id)))[-1] == "done"
        assert state["closed_during_stream"] is False  # the answer could still be stored
        assert state["closed"] is True  # and the session is closed once the response is over
        assert session.added[1].role == "assistant"


# ── errors before the stream starts are ordinary JSON ───────────────────────


class TestBeforeTheStream:
    def _nothing_happened(self, session: AskSession, model: RecordingChatModel) -> None:
        assert session.added == [] and session.commits == 0 and model.received == []
        assert limits_mod.slots_in_use() == 0

    @pytest.mark.parametrize("question", ["", "   ", "\x01\x02"])
    def test_a_blank_question_is_422_json(self, make_client: Any, question: str) -> None:
        conv, model = conversation(), recording_chat_model("x")
        session = AskSession(conv)
        response = stream(make_client(session, graph_for(model)), conv.id, question)
        assert_error(response, 422, "validation_error")
        assert response.headers["content-type"].startswith("application/json")
        self._nothing_happened(session, model)

    def test_a_missing_field_is_422_json(self, make_client: Any) -> None:
        conv, model = conversation(), recording_chat_model("x")
        session = AskSession(conv)
        client = make_client(session, graph_for(model))
        response = client.post(f"/v1/conversations/{conv.id}/ask/stream", json={})
        assert_error(response, 422, "validation_error")
        self._nothing_happened(session, model)

    def test_a_question_over_the_limit_is_413_json(self, make_client: Any) -> None:
        conv, model = conversation(), recording_chat_model("x")
        session = AskSession(conv)
        response = stream(
            make_client(session, graph_for(model)), conv.id, "q" * (MAX_QUESTION_CHARS + 1)
        )
        assert_error(response, 413, "payload_too_large")
        self._nothing_happened(session, model)

    def test_an_unknown_conversation_is_404_json_and_nothing_is_read(
        self, make_client: Any
    ) -> None:
        model = recording_chat_model("x")
        session = AskSession(None)
        response = stream(make_client(session, graph_for(model)), uuid4())
        assert_error(response, 404, "not_found")
        assert session.statements == []
        self._nothing_happened(session, model)

    def test_a_busy_server_is_429_json_before_anything_is_stored(
        self, make_client: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("AI_MAX_CONCURRENT_ASKS", "1")
        conv, model = conversation(), recording_chat_model("x")
        session = AskSession(conv)
        client = make_client(session, graph_for(model))
        with limits_mod.ask_slot(1):
            response = stream(client, conv.id)
        assert_error(response, 429, "busy")
        assert response.headers["content-type"].startswith("application/json")
        assert session.added == [] and session.statements == [] and model.received == []

    def test_a_404_wins_over_a_busy_server(self, make_client: Any, monkeypatch: Any) -> None:
        monkeypatch.setenv("AI_MAX_CONCURRENT_ASKS", "1")
        client = make_client(AskSession(None), graph_for(recording_chat_model("x")))
        with limits_mod.ask_slot(1):
            assert_error(stream(client, uuid4()), 404, "not_found")

    def test_a_failure_while_storing_the_question_is_a_500_and_frees_the_slot(
        self, make_client: Any
    ) -> None:
        conv = conversation()
        session = AskSession(conv)

        async def failing_commit() -> None:
            raise RuntimeError(f"driver said: {SECRET_PROVIDER}")

        session.commit = failing_commit  # type: ignore[method-assign]
        client = make_client(session, graph_for(recording_chat_model("x")))
        response = stream(client, conv.id)
        assert_error(response, 500, "internal_error")
        assert SECRET_PROVIDER not in response.text
        assert limits_mod.slots_in_use() == 0


# ── failures after the stream has started are `error` frames ────────────────


class MidStreamBreak(RecordingChatModel):
    """Streams two chunks, then fails the way a dropped connection does."""

    def _stream(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        yield ChatGenerationChunk(message=AIMessageChunk(content="par"))
        yield ChatGenerationChunk(message=AIMessageChunk(content="tial "))
        raise TimeoutError(f"provider said: {SECRET_PROVIDER}")


class TestErrorFrames:
    def _assert_question_kept_answer_not(self, session: AskSession) -> None:
        assert [m.role for m in session.added] == ["user"]
        assert session.commits == 1
        assert limits_mod.slots_in_use() == 0

    def _error(self, response: Any, frames: list[tuple[str, dict]]) -> dict:
        assert response.status_code == 200  # the status was sent before the failure
        assert frames[-1][0] == "error" and "done" not in kinds(frames)
        data = frames[-1][1]
        assert set(data) == {"error_kind", "error", "trace_id"}
        assert TRACE_RE.match(data["trace_id"])
        assert data["trace_id"] == response.headers["x-trace-id"]
        assert SECRET_PROVIDER not in response.text and SECRET_QUESTION not in response.text
        return data

    def test_a_model_failure_ends_the_stream_with_an_error_frame(self, make_client: Any) -> None:
        conv = conversation()
        session = AskSession(conv)
        client = make_client(session, graph_for(BoomModel(messages=iter([]))))
        response = stream(client, conv.id, SECRET_QUESTION, headers={"x-trace-id": INBOUND})
        frames = sse_frames(response)
        data = self._error(response, frames)
        assert data["error"] == "ai_unavailable" and data["error_kind"] == "error"
        assert data["trace_id"] == INBOUND
        assert kinds(frames) == ["sources", "error"]
        self._assert_question_kept_answer_not(session)

    def test_a_failure_after_some_tokens_keeps_them_but_stores_no_answer(
        self, make_client: Any
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        client = make_client(session, graph_for(MidStreamBreak(messages=iter([]))))
        response = stream(client, conv.id)
        frames = sse_frames(response)
        data = self._error(response, frames)
        assert data["error_kind"] == "timeout" and data["error"] == "ai_unavailable"
        assert "token" in kinds(frames) and text_of(frames) == "partial "
        self._assert_question_kept_answer_not(session)

    def test_a_search_failure_is_an_error_frame_before_any_source(self, make_client: Any) -> None:
        conv = conversation()
        session = AskSession(conv)
        graph = graph_for(recording_chat_model("x"), retriever=BoomRetriever())
        response = stream(make_client(session, graph), conv.id)
        frames = sse_frames(response)
        data = self._error(response, frames)
        assert kinds(frames) == ["error"] and data["error_kind"] == "network"
        self._assert_question_kept_answer_not(session)

    def test_an_unconfigured_ai_layer_is_an_error_frame(self, make_client: Any) -> None:
        from app.ai.graph import build_graph

        conv = conversation()
        session = AskSession(conv)
        graph = build_graph(chat_model=None, retriever=FakeRetriever(CHUNKS), deployment_label="t")
        response = stream(make_client(session, graph), conv.id)  # no AZURE_AI_* set
        frames = sse_frames(response)
        data = self._error(response, frames)
        assert data["error"] == "ai_unavailable"
        self._assert_question_kept_answer_not(session)

    @pytest.mark.parametrize("blank", ["   ", "\x00\x00 \n"])
    def test_a_blank_answer_is_an_error_frame_not_an_empty_message(
        self, make_client: Any, blank: str
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        response = stream(make_client(session, graph_for(recording_chat_model(blank))), conv.id)
        frames = sse_frames(response)
        data = self._error(response, frames)
        assert data["error_kind"] == "empty_answer"
        self._assert_question_kept_answer_not(session)

    def test_a_database_failure_while_storing_the_answer_is_an_error_frame_not_an_exception(
        self, make_client: Any
    ) -> None:
        conv = conversation()

        class Session(AskSession):
            rolled_back = 0

            async def commit(self) -> None:
                if self.commits >= 1:  # the question's commit worked; the answer's does not
                    raise RuntimeError(f"driver said: {SECRET_PROVIDER} {SECRET_ANSWER}")
                await super().commit()

            async def rollback(self) -> None:
                self.rolled_back += 1

        session = Session(conv)
        make_client(session, graph_for(recording_chat_model(f"{SECRET_ANSWER} [1]")))
        # exceptions are NOT swallowed by the client here: one escaping the stream would raise
        client = TestClient(app, raise_server_exceptions=True)
        with structlog.testing.capture_logs() as logs:
            response = stream(client, conv.id, SECRET_QUESTION)
        frames = sse_frames(response)
        data = self._error(response, frames)
        assert (data["error"], data["error_kind"]) == ("internal_error", "persist_failed")
        # the error frame carries a code and a kind — no answer text, no driver text
        assert SECRET_ANSWER not in json.dumps(data) and SECRET_PROVIDER not in json.dumps(data)
        assert "done" not in kinds(frames) and session.rolled_back == 1
        rendered = repr(logs)
        assert SECRET_PROVIDER not in rendered and SECRET_ANSWER not in rendered
        assert "stream persist failed" in rendered
        assert limits_mod.slots_in_use() == 0


# ── the cap on the answer ────────────────────────────────────────────────────


class TestCap:
    def _answer_of_length(self, n: int) -> str:
        words, total = [], 0
        while total < n:
            word = "word"[: max(1, min(4, n - total - 1))]
            words.append(word)
            total += len(word) + 1
        return " ".join(words)[:n]

    def test_a_long_answer_is_cut_at_the_limit_and_the_stream_stops_there(
        self, make_client: Any
    ) -> None:
        conv = conversation()
        session = AskSession(conv)
        long_answer = "word " * 5000
        client = make_client(session, graph_for(recording_chat_model(long_answer)))
        with structlog.testing.capture_logs() as logs:
            frames = sse_frames(stream(client, conv.id))
        streamed = text_of(frames)
        assert len(streamed) == MAX_ANSWER_CHARS and streamed.endswith("…")
        assert session.added[1].content == streamed  # what was shown is what is stored
        assert kinds(frames)[-1] == "done"
        assert frames[-1][1]["output_tokens"] is None  # the model was not read to the end
        assert next(e for e in logs if e["event"] == "question answered")["cut"] is True

    def test_an_answer_that_fits_is_streamed_whole(self, make_client: Any) -> None:
        conv = conversation()
        session = AskSession(conv)
        answer = self._answer_of_length(MAX_ANSWER_CHARS - 2)
        client = make_client(session, graph_for(recording_chat_model(answer)))
        frames = sse_frames(stream(client, conv.id))
        assert text_of(frames) == answer == session.added[1].content
        assert "…" not in text_of(frames)

    def test_a_marker_cut_off_by_the_limit_is_not_a_citation(self, make_client: Any) -> None:
        conv = conversation()
        client = make_client(
            AskSession(conv),
            graph_for(recording_chat_model("word " * 5000 + "cited at the end [2]")),
        )
        assert sse_frames(stream(client, conv.id))[-1][1]["citations"] == []


class TestModelIteratorClosing:
    def test_the_graph_stream_is_closed_as_soon_as_the_answer_is_cut(self) -> None:
        conv = conversation()
        session = AskSession(conv)
        closed: list[int] = []

        class ManyTokens:
            async def astream(
                self, inputs: Any, config: Any = None, stream_mode: Any = None
            ) -> Any:
                try:
                    yield "updates", {"retrieve": {"context": [CHUNKS[0]]}}
                    for _ in range(100):
                        chunk = AIMessageChunk(content="x" * 1000)
                        yield "messages", (chunk, {"langgraph_node": "answer"})
                finally:
                    closed.append(1)

        async def scenario() -> list[int]:
            history = await ask_mod._store_question(conv.id, "q", session)
            frames = ask_mod._answer_frames(conv.id, "q", history, session, ManyTokens())
            at_done: list[int] = []
            async for frame in frames:
                if frame.startswith("event: done"):
                    at_done = list(closed)  # the generator is still running here
            return at_done

        assert asyncio.run(scenario()) == [
            1
        ]  # closed before `done`, not left to garbage collection


# ── the concurrency slot ─────────────────────────────────────────────────────


class GatedStreamGraph:
    """A graph whose ``astream`` pauses after retrieval until the test lets it go."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def astream(self, inputs: Any, config: Any = None, stream_mode: Any = None) -> Any:
        yield "updates", {"retrieve": {"context": [CHUNKS[0]]}}
        self.started.set()
        await self.gate.wait()
        yield "messages", (AIMessageChunk(content="done [1]"), {"langgraph_node": "answer"})
        yield "updates", {"answer": {"messages": [AIMessage(content="done [1]")]}}


class TestSlot:
    def test_a_stream_holds_a_slot_until_it_ends_and_a_second_one_is_refused(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("AI_MAX_CONCURRENT_ASKS", "1")
        conv = conversation()
        session = AskSession(conv)
        graph = GatedStreamGraph()

        async def override() -> Any:
            yield session

        app.dependency_overrides[get_session] = override
        app.dependency_overrides[get_answer_graph] = lambda: graph

        async def scenario() -> tuple[int, int, int, int, str]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
                url = f"/v1/conversations/{conv.id}/ask/stream"
                first = asyncio.create_task(http.post(url, json={"question": "one"}))
                await asyncio.wait_for(graph.started.wait(), timeout=5)
                held = limits_mod.slots_in_use()
                second = await asyncio.wait_for(http.post(url, json={"question": "two"}), timeout=5)
                still_held = limits_mod.slots_in_use()
                graph.gate.set()
                finished = await asyncio.wait_for(first, timeout=5)
                return (
                    held,
                    second.status_code,
                    still_held,
                    limits_mod.slots_in_use(),
                    finished.text,
                )

        held, second_status, still_held, after, body = asyncio.run(scenario())
        assert (held, second_status, still_held, after) == (1, 429, 1, 0)
        assert body.count("event: done") == 1
        assert [m.role for m in session.added].count("user") == 1  # the refused one stored nothing

    def test_the_slot_is_released_after_a_success_and_after_every_kind_of_failure(
        self, make_client: Any
    ) -> None:
        conv = conversation()
        for model in (
            recording_chat_model("ok [1]"),
            BoomModel(messages=iter([])),
            MidStreamBreak(messages=iter([])),
            recording_chat_model("  "),
        ):
            stream(make_client(AskSession(conv), graph_for(model)), conv.id)
            assert limits_mod.slots_in_use() == 0

    def test_the_response_releases_its_slot_exactly_once_even_when_the_client_leaves(self) -> None:
        released: list[int] = []
        closed: list[int] = []

        async def frames() -> Any:
            try:
                yield "event: sources\ndata: {}\n\n"
                await asyncio.sleep(30)  # waiting for the model
                yield "event: done\ndata: {}\n\n"
            finally:
                closed.append(1)

        async def scenario() -> list[Any]:
            response = SseResponse(frames(), on_close=lambda: released.append(1))
            sent: list[Any] = []

            async def send(message: Any) -> None:
                sent.append(message)

            async def receive() -> Any:
                await asyncio.sleep(30)  # a client that never disconnects in the ASGI sense
                return {"type": "http.disconnect"}

            scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
            task = asyncio.create_task(response(scope, receive, send))
            while not any(m.get("body") for m in sent):
                await asyncio.sleep(0.01)
            task.cancel()  # what the server does when the connection goes away
            with pytest.raises(asyncio.CancelledError):
                await task
            return sent

        asyncio.run(asyncio.wait_for(scenario(), timeout=10))
        assert released == [1] and closed == [1]

    def test_a_generator_waiting_at_a_yield_is_closed_when_the_client_leaves(self) -> None:
        """The client goes while the server is blocked sending a frame: the generator is suspended
        at its ``yield`` — nothing is cancelled inside it. Only an explicit close runs its cleanup;
        without one it would wait for the garbage collector."""
        released: list[int] = []
        closed: list[int] = []

        async def frames() -> Any:
            try:
                yield "event: sources\ndata: {}\n\n"
                yield "event: done\ndata: {}\n\n"
            finally:
                closed.append(1)

        async def scenario() -> list[int]:
            body = frames()
            response = SseResponse(body, on_close=lambda: released.append(1))
            entered = asyncio.Event()

            async def send(message: Any) -> None:
                if message.get("body"):
                    entered.set()
                    await asyncio.sleep(30)  # a slow or vanished client: the write never completes

            async def receive() -> Any:
                await asyncio.sleep(30)
                return {"type": "http.disconnect"}

            task = asyncio.create_task(
                response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return list(closed)  # the body is still referenced here: no garbage collection

        assert asyncio.run(asyncio.wait_for(scenario(), timeout=10)) == [1]
        assert released == [1]

    def test_a_response_that_ends_normally_also_releases_once(self) -> None:
        released: list[int] = []

        async def frames() -> Any:
            yield "event: done\ndata: {}\n\n"

        async def scenario() -> None:
            response = SseResponse(frames(), on_close=lambda: released.append(1))

            async def send(message: Any) -> None:
                return None

            async def receive() -> Any:
                await asyncio.sleep(30)
                return {"type": "http.disconnect"}

            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

        asyncio.run(asyncio.wait_for(scenario(), timeout=10))
        assert released == [1]

    def test_a_client_that_leaves_before_the_answer_is_complete_stores_no_answer(self) -> None:
        conv = conversation()
        session = AskSession(conv)
        graph = graph_for(recording_chat_model("alpha beta gamma delta [1]"))

        async def scenario() -> None:
            history = await ask_mod._store_question(conv.id, "q", session)
            frames = ask_mod._answer_frames(conv.id, "q", history, session, graph)
            first = await frames.__anext__()
            assert first.startswith("event: sources")
            await frames.aclose()  # the response closes the generator when the client is gone

        asyncio.run(scenario())
        assert [m.role for m in session.added] == ["user"]  # the question stays, nothing else


# ── nothing private leaves the process ───────────────────────────────────────


class TestNoContentInLogs:
    def _logs(self, make_client: Any, model: Any) -> str:
        conv = conversation()
        client = make_client(AskSession(conv), graph_for(model))
        with structlog.testing.capture_logs() as logs:
            stream(client, conv.id, SECRET_QUESTION)
        return repr(logs)

    def test_a_good_stream_logs_no_question_answer_passage_title_or_path(
        self, make_client: Any
    ) -> None:
        rendered = self._logs(make_client, recording_chat_model(f"{SECRET_ANSWER} [1]"))
        for private in (
            SECRET_QUESTION,
            SECRET_ANSWER,
            SECRET_PASSAGE,
            "First doc",
            "docs/doc1.md",
        ):
            assert private not in rendered
        assert "question answered" in rendered and "answer citations" in rendered

    def test_the_counts_are_logged_once(self, make_client: Any) -> None:
        conv = conversation()
        client = make_client(AskSession(conv), graph_for(recording_chat_model("Yes [1] and [7].")))
        with structlog.testing.capture_logs() as logs:
            stream(client, conv.id)
        lines = [e for e in logs if e["event"] == "answer citations"]
        assert len(lines) == 1
        assert (lines[0]["retrieved"], lines[0]["cited"], lines[0]["invalid"]) == (3, 1, 1)

    @pytest.mark.parametrize("model_name", ["boom", "midstream"])
    def test_a_failed_stream_logs_a_kind_and_none_of_the_content(
        self, make_client: Any, model_name: str
    ) -> None:
        model = (
            BoomModel(messages=iter([]))
            if model_name == "boom"
            else MidStreamBreak(messages=iter([]))
        )
        rendered = self._logs(make_client, model)
        assert "ask failed" in rendered
        for private in (SECRET_QUESTION, SECRET_PROVIDER, SECRET_PASSAGE):
            assert private not in rendered
