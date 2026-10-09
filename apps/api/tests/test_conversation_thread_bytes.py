"""A thread read is bounded by bytes as well as by count (api roadmap 5.4) — offline.

500 messages of 4,000 four-byte characters with long citation titles are about 10 MB; the web
BFF refuses an upstream body over 8 MiB, so the conversation could not be opened. The read now
stops at about 4 MiB of message text (UTF-8 bytes of content and citations), keeping the most
recent messages.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mssql

from app.db import get_session
from app.main import app
from app.models.conversation import Conversation, Message
from app.repositories import conversations as repo

BFF_LIMIT_BYTES = 8 * 1024 * 1024
EMOJI = "\U0001f600"  # 4 bytes in UTF-8, 2 UTF-16 units
LONG_TITLE = EMOJI * 200  # the title column's maximum, all four-byte characters


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """Just enough of ``AsyncSession``: the conversation, and the rows one query returns."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []
        self.known: dict[UUID, Conversation] = {}

    async def get(self, model: type, key: UUID) -> Any:
        return self.known.get(key)

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self.rows)


def _conversation() -> Conversation:
    moment = datetime.fromisoformat("2026-10-06T10:00:00")
    return Conversation(id=uuid4(), title="Big thread", created_at=moment, updated_at=moment)


def _message(seq: int, content: str, citations: list[dict[str, str]] | None = None) -> Message:
    return Message(
        id=uuid4(),
        seq=seq,
        conversation_id=uuid4(),
        role="assistant" if citations else "user",
        content=content,
        citations=citations,
        created_at=datetime.fromisoformat("2026-10-06T10:00:00.123000"),
    )


def _fat_citations(count: int = 5) -> list[dict[str, str]]:
    return [{"title": LONG_TITLE, "path": f"docs/{EMOJI * 100}/{i}.md"} for i in range(count)]


def _worst_thread(rows: int = 501) -> list[Message]:
    """The newest ``rows`` messages, newest first: 4,000 emoji each, with fat citations."""
    return [_message(seq, EMOJI * 4000, _fat_citations()) for seq in range(rows, 0, -1)]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _read(rows: list[Any], **kwargs: Any) -> repo.Thread:
    session = FakeSession(rows)
    conversation = _conversation()
    session.known[conversation.id] = conversation
    thread = _run(repo.get_conversation_with_messages(session, conversation.id, **kwargs))
    assert thread is not None and len(session.statements) == 1
    return thread


# ── how bytes are counted ────────────────────────────────────────────────────


class TestWireBytes:
    def test_it_counts_utf8_bytes_not_characters(self) -> None:
        small = repo.message_wire_bytes(SimpleNamespace(content="a" * 4000, citations=None))  # type: ignore[arg-type]
        emoji = repo.message_wire_bytes(SimpleNamespace(content=EMOJI * 4000, citations=None))  # type: ignore[arg-type]
        assert emoji - small == 4000 * 3  # 4 bytes against 1 for each of 4,000 characters
        assert emoji >= 4000 * 4

    def test_it_counts_citations(self) -> None:
        bare = repo.message_wire_bytes(SimpleNamespace(content="x", citations=None))  # type: ignore[arg-type]
        cited = repo.message_wire_bytes(
            SimpleNamespace(content="x", citations=_fat_citations(5))  # type: ignore[arg-type]
        )
        assert cited - bare >= 5 * (200 * 4)  # at least the five titles, four bytes a character

    def test_it_counts_what_json_escaping_adds(self) -> None:
        plain = repo.message_wire_bytes(SimpleNamespace(content="a" * 100, citations=None))  # type: ignore[arg-type]
        quoted = repo.message_wire_bytes(SimpleNamespace(content='"' * 100, citations=None))  # type: ignore[arg-type]
        assert quoted - plain == 100  # each quote is written as \"

    def test_it_matches_the_json_a_response_would_hold(self) -> None:
        message = SimpleNamespace(content="héllo " + EMOJI, citations=[{"title": "T", "path": "p"}])
        text = json.dumps(
            [message.content, message.citations], ensure_ascii=False, separators=(",", ":")
        )
        assert repo.message_wire_bytes(message) == len(text.encode()) + repo.MESSAGE_OVERHEAD_BYTES  # type: ignore[arg-type]

    def test_the_defaults_are_about_4_mib_and_leave_room_under_the_bff_limit(self) -> None:
        assert repo.MAX_THREAD_BYTES == 4 * 1024 * 1024
        assert repo.MAX_THREAD_BYTES * 2 <= BFF_LIMIT_BYTES


AWKWARD_TEXTS = [
    "plain",
    'quotes " and \\ backslashes',
    "tab\tnewline\ncarriage\rformfeed\fbackspace\b",
    "bell\x07 escape\x1b unit-separator\x1f nul\x00 del\x7f",
    "line\u2028separator and paragraph\u2029",
    "caf\u00e9 \u65e5\u672c\u8a9e " + EMOJI * 10,
    "\x00" * 50,
    '"' * 50 + "\\" * 50,
]


class TestTheCountNeverUnderstates:
    @pytest.mark.parametrize("text", AWKWARD_TEXTS)
    def test_the_count_is_at_least_the_bytes_the_response_holds(self, text: str) -> None:
        from app.routers.conversations import MessageOut

        message = _message(1, text, [{"title": text[:30], "path": "docs/a.md"}])
        written = MessageOut.model_validate(message).model_dump_json().encode("utf-8")
        counted = repo.message_wire_bytes(message)
        assert counted >= len(written), (counted, len(written))
        assert counted - len(written) < 200  # and it is not wildly generous


# ── the read ─────────────────────────────────────────────────────────────────


class TestBoundedRead:
    def test_the_worst_thread_returns_fewer_messages_under_the_bound(self) -> None:
        rows = _worst_thread()
        assert sum(repo.message_wire_bytes(m) for m in rows[:500]) > BFF_LIMIT_BYTES  # the problem
        thread = _read(rows)
        assert thread.truncated is True
        assert 0 < len(thread.messages) < 500
        total = sum(repo.message_wire_bytes(m) for m in thread.messages)
        assert total <= repo.MAX_THREAD_BYTES

    def test_it_keeps_the_most_recent_messages_oldest_first(self) -> None:
        thread = _read(_worst_thread())
        seqs = [m.seq for m in thread.messages]
        assert seqs == sorted(seqs) and seqs[-1] == 501  # the newest is there, in order
        assert seqs == list(range(seqs[0], 502))  # no gaps: a contiguous tail of the thread

    def test_it_keeps_as_many_as_fit_and_no_fewer(self) -> None:
        rows = _worst_thread()
        thread = _read(rows)
        kept = len(thread.messages)
        used = sum(repo.message_wire_bytes(m) for m in thread.messages)
        assert used + repo.message_wire_bytes(rows[kept]) > repo.MAX_THREAD_BYTES

    def test_a_small_thread_is_untouched(self) -> None:
        rows = [_message(seq, f"message {seq}") for seq in (3, 2, 1)]
        thread = _read(rows)
        assert [m.seq for m in thread.messages] == [1, 2, 3] and thread.truncated is False

    def test_exactly_at_the_bound_is_not_truncated_and_one_byte_less_is(self) -> None:
        rows = [_message(seq, f"message {seq}" * 20) for seq in (3, 2, 1)]
        total = sum(repo.message_wire_bytes(m) for m in rows)
        exact = _read(rows, max_bytes=total)
        assert [m.seq for m in exact.messages] == [1, 2, 3] and exact.truncated is False
        less = _read(rows, max_bytes=total - 1)
        assert [m.seq for m in less.messages] == [2, 3] and less.truncated is True

    def test_it_stops_at_the_first_message_that_does_not_fit_and_leaves_no_gap(self) -> None:
        # newest first: small, HUGE, small, small. The small ones would fit after the huge one is
        # skipped, but a thread with a hole in it is worse than one that is cut: stop there.
        rows = [
            _message(4, "a" * 100),
            _message(3, "b" * 5000),
            _message(2, "c" * 100),
            _message(1, "d" * 100),
        ]
        small = repo.message_wire_bytes(rows[0])
        thread = _read(rows, max_bytes=small * 3)
        assert [m.seq for m in thread.messages] == [4] and thread.truncated is True

    def test_the_newest_message_is_always_returned_even_over_the_bound(self) -> None:
        rows = [_message(2, "a" * 500), _message(1, "b" * 500)]
        thread = _read(rows, max_bytes=1)
        assert [m.seq for m in thread.messages] == [2] and thread.truncated is True

    def test_a_lone_message_over_the_bound_is_not_a_truncation(self) -> None:
        thread = _read([_message(1, "a" * 500)], max_bytes=1)
        assert [m.seq for m in thread.messages] == [1] and thread.truncated is False

    def test_the_count_bound_still_works_and_the_two_combine(self) -> None:
        rows = [_message(seq, "m") for seq in (5, 4, 3, 2)]  # limit + 1 rows
        assert _read(rows, limit=3).truncated is True
        # by count AND by bytes: still the newest that fit, flagged once
        both = _read(rows, limit=3, max_bytes=repo.message_wire_bytes(rows[0]) * 2)
        assert [m.seq for m in both.messages] == [4, 5] and both.truncated is True

    def test_it_reads_no_more_rows_than_before(self) -> None:
        session = FakeSession(_worst_thread())
        conversation = _conversation()
        session.known[conversation.id] = conversation
        _run(repo.get_conversation_with_messages(session, conversation.id))
        assert len(session.statements) == 1  # one query, not one per byte check
        sql = str(
            session.statements[0].compile(
                dialect=mssql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert f"TOP {repo.MAX_THREAD_MESSAGES + 1}" in sql

    @pytest.mark.parametrize("max_bytes", [0, -1])
    def test_a_bound_below_one_is_refused_before_any_query(self, max_bytes: int) -> None:
        session = FakeSession([])
        with pytest.raises(ValueError):
            _run(repo.get_conversation_with_messages(session, uuid4(), max_bytes=max_bytes))
        assert session.statements == []


# ── the endpoint ─────────────────────────────────────────────────────────────


@pytest.fixture()
def client_for() -> Any:
    def build(rows: list[Any]) -> tuple[TestClient, Conversation, FakeSession]:
        session = FakeSession(rows)
        conversation = _conversation()
        session.known[conversation.id] = conversation

        async def override() -> Any:
            yield session

        app.dependency_overrides[get_session] = override
        return TestClient(app, raise_server_exceptions=False), conversation, session

    yield build
    app.dependency_overrides.pop(get_session, None)


class TestEndpoint:
    def test_the_worst_thread_stays_under_the_bound_on_the_wire(self, client_for: Any) -> None:
        client, conversation, _ = client_for(_worst_thread())
        res = client.get(f"/v1/conversations/{conversation.id}")
        assert res.status_code == 200
        body = res.json()
        assert body["truncated"] is True and 0 < len(body["messages"]) < 500
        # the real response, as written: under the bound, and nowhere near the BFF's limit
        assert len(res.content) <= repo.MAX_THREAD_BYTES
        assert len(res.content) < BFF_LIMIT_BYTES
        assert len(res.content) > repo.MAX_THREAD_BYTES * 0.9  # it is not cutting far too early

    def test_the_messages_are_the_newest_in_order(self, client_for: Any) -> None:
        rows = [
            _message(seq, f"{seq}:" + EMOJI * 4000, _fat_citations()) for seq in range(501, 0, -1)
        ]
        client, conversation, _ = client_for(rows)
        body = client.get(f"/v1/conversations/{conversation.id}").json()
        numbers = [int(m["content"].split(":")[0]) for m in body["messages"]]
        assert numbers == sorted(numbers) and numbers[-1] == 501
        assert numbers == list(range(numbers[0], 502))  # a contiguous tail, oldest first

    def test_a_normal_thread_is_returned_whole(self, client_for: Any) -> None:
        rows = [_message(seq, f"m{seq}") for seq in (3, 2, 1)]
        client, conversation, _ = client_for(rows)
        body = client.get(f"/v1/conversations/{conversation.id}").json()
        assert body["truncated"] is False
        assert [m["content"] for m in body["messages"]] == ["m1", "m2", "m3"]

    def test_the_bound_is_read_at_call_time(
        self, client_for: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [_message(seq, "x" * 1000) for seq in (3, 2, 1)]
        monkeypatch.setattr(repo, "MAX_THREAD_BYTES", 2 * repo.message_wire_bytes(rows[0]))
        client, conversation, _ = client_for(rows)
        body = client.get(f"/v1/conversations/{conversation.id}").json()
        assert body["truncated"] is True and len(body["messages"]) == 2

    def test_the_content_is_still_never_logged(
        self, client_for: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rows = [_message(1, "SECRET-BYTE-BOUND-TEXT")]
        client, conversation, _ = client_for(rows)
        client.get(f"/v1/conversations/{conversation.id}")
        assert "SECRET-BYTE-BOUND-TEXT" not in capsys.readouterr().out


def test_the_openapi_description_names_the_byte_bound_next_to_the_count() -> None:
    schema = app.openapi()["components"]["schemas"]["ConversationDetail"]
    description = schema["properties"]["truncated"]["description"]
    assert "500 messages" in description and "4 MiB" in description and "bytes" in description
