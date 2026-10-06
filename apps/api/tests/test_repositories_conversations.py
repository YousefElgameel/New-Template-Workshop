"""Conversation repository: titles, ordering, the ``updated_at`` bump, validation.

A fake ``AsyncSession`` stands in for the database — nothing connects (rule 40), and the queries
it receives are compiled for SQL Server to assert their ordering and limits.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from itertools import count
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import mssql

from app.models.conversation import DEFAULT_CONVERSATION_TITLE, Conversation, Message
from app.repositories import conversations as repo


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """Just enough of ``AsyncSession`` for the repository."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.added: list[Any] = []
        self.statements: list[Any] = []
        self.flushes = 0
        self.refreshed: list[tuple[Any, list[str] | None]] = []
        self.conversations: dict[UUID, Conversation] = {}
        self._seq = count(1)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Conversation):
            obj.id = obj.id or uuid4()
            self.conversations[obj.id] = obj

    async def flush(self) -> None:
        self.flushes += 1

    async def get(self, model: type, key: UUID) -> Any:
        assert model is Conversation
        return self.conversations.get(key)

    async def refresh(self, obj: Any, attribute_names: list[str] | None = None) -> None:
        self.refreshed.append((obj, attribute_names))
        if isinstance(obj, Message):
            obj.seq = next(self._seq)

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self.rows)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=mssql.dialect()))


def run(coro):
    return asyncio.run(coro)


def _existing(session: FakeSession, title: str = DEFAULT_CONVERSATION_TITLE) -> Conversation:
    conversation = Conversation(id=uuid4(), title=title)
    conversation.created_at = conversation.updated_at = datetime(2026, 1, 1)
    session.conversations[conversation.id] = conversation
    return conversation


# --- titles -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, DEFAULT_CONVERSATION_TITLE),
        ("", DEFAULT_CONVERSATION_TITLE),
        ("   \n\t ", DEFAULT_CONVERSATION_TITLE),
        ("  How does   tracing\nwork? ", "How does tracing work?"),
        ("x" * 250, "x" * 200),
    ],
)
def test_make_title(text: str | None, expected: str) -> None:
    assert repo.make_title(text) == expected


# --- create / list / get ----------------------------------------------------------------------


def test_create_conversation_defaults_and_flushes() -> None:
    session = FakeSession()
    conversation = run(repo.create_conversation(session))
    assert conversation.title == DEFAULT_CONVERSATION_TITLE
    assert conversation.created_at == conversation.updated_at
    assert session.added == [conversation] and session.flushes == 1


def test_create_conversation_uses_a_given_title_cut_to_200() -> None:
    conversation = run(repo.create_conversation(FakeSession(), "t" * 300))
    assert conversation.title == "t" * 200


def test_list_conversations_is_newest_first_and_limited() -> None:
    session = FakeSession(rows=["a", "b"])
    assert run(repo.list_conversations(session, limit=7)) == ["a", "b"]
    sql = _sql(session.statements[0])
    assert "ORDER BY conversations.updated_at DESC" in sql
    assert "TOP" in sql


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_list_conversations_rejects_a_limit_outside_1_100(limit: int) -> None:
    session = FakeSession()
    with pytest.raises(ValueError):
        run(repo.list_conversations(session, limit=limit))
    assert session.statements == []


@pytest.mark.parametrize("limit", [1, 50, 100])
def test_list_conversations_accepts_the_boundaries(limit: int) -> None:
    run(repo.list_conversations(FakeSession(), limit=limit))


def test_get_conversation_returns_none_for_an_unknown_id() -> None:
    assert run(repo.get_conversation(FakeSession(), uuid4())) is None


def test_get_with_messages_orders_the_thread_by_seq() -> None:
    session = FakeSession(rows=["m1", "m2"])
    conversation = _existing(session)
    found = run(repo.get_conversation_with_messages(session, conversation.id))
    assert found is not None
    assert found[0] is conversation and found[1] == ["m1", "m2"]
    sql = _sql(session.statements[0])
    assert "ORDER BY messages.seq" in sql and "messages.conversation_id" in sql


def test_get_with_messages_returns_none_when_unknown() -> None:
    session = FakeSession()
    assert run(repo.get_conversation_with_messages(session, uuid4())) is None
    assert session.statements == []


def test_recent_messages_are_the_last_n_oldest_first() -> None:
    # The query returns newest-first (DESC + LIMIT); the repository flips them for the model.
    session = FakeSession(rows=["m3", "m2", "m1"])
    assert run(repo.get_recent_messages(session, uuid4(), limit=3)) == ["m1", "m2", "m3"]
    sql = _sql(session.statements[0])
    assert "ORDER BY messages.seq DESC" in sql and "TOP" in sql


# --- add_message ------------------------------------------------------------------------------


def test_add_message_stores_it_and_loads_the_identity_seq() -> None:
    session = FakeSession()
    conversation = _existing(session)
    message = run(
        repo.add_message(
            session,
            conversation.id,
            "assistant",
            "an answer",
            citations=[{"title": "T", "path": "docs/a.md"}],
            token_count=42,
        )
    )
    assert message in session.added
    assert message.seq == 1
    assert message.citations == [{"title": "T", "path": "docs/a.md"}]
    assert message.token_count == 42
    assert session.refreshed == [(message, ["seq"])]


def test_add_message_bumps_updated_at() -> None:
    session = FakeSession()
    conversation = _existing(session)
    before = conversation.updated_at
    run(repo.add_message(session, conversation.id, "assistant", "hi"))
    assert conversation.updated_at > before
    assert conversation.updated_at - before > timedelta(0)


def test_first_user_message_becomes_the_title() -> None:
    session = FakeSession()
    conversation = _existing(session)
    run(repo.add_message(session, conversation.id, "user", "How does  tracing work?"))
    assert conversation.title == "How does tracing work?"


def test_a_later_user_message_does_not_change_the_title() -> None:
    session = FakeSession()
    conversation = _existing(session)
    run(repo.add_message(session, conversation.id, "user", "first question"))
    run(repo.add_message(session, conversation.id, "user", "second question"))
    assert conversation.title == "first question"


def test_an_assistant_message_never_sets_the_title() -> None:
    session = FakeSession()
    conversation = _existing(session)
    run(repo.add_message(session, conversation.id, "assistant", "an answer"))
    assert conversation.title == DEFAULT_CONVERSATION_TITLE


def test_an_explicit_title_is_kept() -> None:
    session = FakeSession()
    conversation = _existing(session, title="My topic")
    run(repo.add_message(session, conversation.id, "user", "a question"))
    assert conversation.title == "My topic"


def test_a_blank_first_question_keeps_the_default_title() -> None:
    session = FakeSession()
    conversation = _existing(session)
    run(repo.add_message(session, conversation.id, "user", "   "))
    assert conversation.title == DEFAULT_CONVERSATION_TITLE


def test_add_message_to_an_unknown_conversation_raises_not_found() -> None:
    session = FakeSession()
    with pytest.raises(repo.ConversationNotFound):
        run(repo.add_message(session, uuid4(), "user", "hello"))
    assert session.added == [] and session.flushes == 0


@pytest.mark.parametrize("role", ["system", "tool", "", "User"])
def test_add_message_rejects_an_invalid_role(role: str) -> None:
    session = FakeSession()
    conversation = _existing(session)
    with pytest.raises(ValueError):
        run(repo.add_message(session, conversation.id, role, "x"))
    assert session.added == []


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_recent_messages_rejects_a_bad_limit(limit: int) -> None:
    session = FakeSession()
    with pytest.raises(ValueError):
        run(repo.get_recent_messages(session, uuid4(), limit=limit))
    assert session.statements == []


def test_list_orders_ties_by_id_for_a_stable_page() -> None:
    session = FakeSession()
    run(repo.list_conversations(session))
    assert "conversations.id DESC" in _sql(session.statements[0])


def test_the_message_and_the_conversation_share_one_timestamp() -> None:
    session = FakeSession()
    conversation = _existing(session)
    message = run(repo.add_message(session, conversation.id, "assistant", "hi"))
    assert message.created_at == conversation.updated_at
