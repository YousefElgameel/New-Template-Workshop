"""``/v1/conversations`` — create, list, open (roadmap api 2.1).

The real FastAPI app with ``get_session`` overridden by a fake session: no database, no network
(rule 40). The repository runs for real, so the SQL it would send is compiled and asserted too.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pyodbc
import pytest
import structlog.testing
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mssql

import app.repositories.conversations as repo
import app.tracing as tracing_mod
from app.db import get_session
from app.main import app
from app.models.conversation import DEFAULT_CONVERSATION_TITLE, Conversation, Message

TRACE_RE = re.compile(r"^[0-9a-f]{32}$")
INBOUND = "0eb01" + "c" * 27
SECRET_TITLE = "SECRET-TITLE-TEXT"
SECRET_MESSAGE = "SECRET-MESSAGE-TEXT"


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """Just enough of ``AsyncSession``; ``results`` is consumed one entry per ``execute``."""

    def __init__(self, results: list[list[Any]] | None = None) -> None:
        self.results = list(results or [])
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.known: dict[UUID, Conversation] = {}
        self.commits = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Conversation):
            obj.id = obj.id or uuid4()
            self.known[obj.id] = obj

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def get(self, model: type, key: UUID) -> Any:
        return self.known.get(key)

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self.results.pop(0) if self.results else [])


@pytest.fixture()
def make_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """``make_client(session)`` → a TestClient whose requests use that fake session."""
    monkeypatch.setattr(pyodbc, "connect", lambda *a, **k: pytest.fail("must not connect"))

    def build(session: FakeSession) -> TestClient:
        async def override() -> Any:
            yield session

        app.dependency_overrides[get_session] = override
        return TestClient(app, raise_server_exceptions=False)

    yield build
    app.dependency_overrides.pop(get_session, None)


def _conversation(title: str = "A topic", updated: str = "2026-10-06T10:00:00") -> Conversation:
    moment = datetime.fromisoformat(updated)
    return Conversation(id=uuid4(), title=title, created_at=moment, updated_at=moment)


def _message(
    seq: int, role: str, content: str, citations: list[dict[str, str]] | None = None
) -> Message:
    return Message(
        id=uuid4(),
        seq=seq,
        conversation_id=uuid4(),
        role=role,
        content=content,
        citations=citations,
        created_at=datetime.fromisoformat("2026-10-06T10:00:00.123000"),
    )


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=mssql.dialect(), compile_kwargs={"literal_binds": True}))


def _assert_error(res: Any, status: int, code: str) -> None:
    assert res.status_code == status
    body = res.json()
    assert set(body) == {"error", "trace_id"} and body["error"] == code
    assert TRACE_RE.match(body["trace_id"]) and res.headers["x-trace-id"] == body["trace_id"]


# --- POST /v1/conversations -----------------------------------------------------------------------


def test_create_without_a_body_returns_201_with_the_default_title(make_client: Any) -> None:
    session = FakeSession()
    res = make_client(session).post("/v1/conversations")
    assert res.status_code == 201
    body = res.json()
    assert set(body) == {"id", "title", "created_at", "updated_at"}
    assert UUID(body["id"]) and body["title"] == DEFAULT_CONVERSATION_TITLE
    assert body["created_at"].endswith("Z") and body["updated_at"].endswith("Z")  # explicit UTC
    assert session.commits == 1  # the write is committed by the endpoint, once
    assert TRACE_RE.match(res.headers["x-trace-id"])


def test_create_with_a_title_trims_and_collapses_whitespace(make_client: Any) -> None:
    session = FakeSession()
    res = make_client(session).post("/v1/conversations", json={"title": "  How   does\ttracing  "})
    assert res.status_code == 201 and res.json()["title"] == "How does tracing"


@pytest.mark.parametrize("title", ["", "   ", None])
def test_create_with_a_blank_title_uses_the_default(make_client: Any, title: str | None) -> None:
    res = make_client(FakeSession()).post("/v1/conversations", json={"title": title})
    assert res.status_code == 201 and res.json()["title"] == DEFAULT_CONVERSATION_TITLE


def test_create_accepts_a_title_of_exactly_200_characters(make_client: Any) -> None:
    res = make_client(FakeSession()).post("/v1/conversations", json={"title": "t" * 200})
    assert res.status_code == 201 and res.json()["title"] == "t" * 200


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "t" * 201},
        {"title": 123},
        {"title": ["a"]},
        {"unknown": "field"},
        ["not", "an", "object"],
    ],
)
def test_create_rejects_a_bad_body_with_422(make_client: Any, payload: Any) -> None:
    session = FakeSession()
    res = make_client(session).post("/v1/conversations", json=payload)
    _assert_error(res, 422, "validation_error")
    assert session.commits == 0 and session.added == []  # nothing was written


@pytest.mark.parametrize("content", [None, "", "null", "{}"])
def test_create_accepts_an_empty_null_or_empty_object_body(
    make_client: Any, content: str | None
) -> None:
    client = make_client(FakeSession())
    headers = {"content-type": "application/json"}
    res = client.post("/v1/conversations", content=content, headers=headers if content else None)
    assert res.status_code == 201 and res.json()["title"] == DEFAULT_CONVERSATION_TITLE


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"a string"', "123"])
def test_create_rejects_malformed_json_or_a_non_object_with_422(
    make_client: Any, content: str
) -> None:
    res = make_client(FakeSession()).post(
        "/v1/conversations", content=content, headers={"content-type": "application/json"}
    )
    _assert_error(res, 422, "validation_error")


def test_create_measures_the_title_in_utf16_units_like_the_column(make_client: Any) -> None:
    """NVARCHAR(200) counts UTF-16 units: an emoji is 2, so 100 fit and 101 do not."""
    ok = make_client(FakeSession()).post("/v1/conversations", json={"title": "\U0001f600" * 100})
    assert ok.status_code == 201 and ok.json()["title"] == "\U0001f600" * 100
    too_long = make_client(FakeSession()).post(
        "/v1/conversations", json={"title": "\U0001f600" * 101}
    )
    _assert_error(too_long, 422, "validation_error")  # a clean 422, not a database overflow


def test_create_does_not_echo_the_rejected_value(make_client: Any) -> None:
    res = make_client(FakeSession()).post(
        "/v1/conversations", json={"title": "x" * 201 + SECRET_TITLE}
    )
    _assert_error(res, 422, "validation_error")
    assert SECRET_TITLE not in res.text


# --- GET /v1/conversations ------------------------------------------------------------------------


def test_list_returns_items_and_a_count(make_client: Any) -> None:
    rows = [_conversation("Newer", "2026-10-06T11:00:00"), _conversation("Older")]
    session = FakeSession([rows])
    res = make_client(session).get("/v1/conversations")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 2 and [i["title"] for i in body["items"]] == ["Newer", "Older"]
    assert set(body["items"][0]) == {"id", "title", "updated_at"}
    assert body["items"][0]["updated_at"] == "2026-10-06T11:00:00Z"


def test_list_defaults_to_50_newest_first(make_client: Any) -> None:
    session = FakeSession([[]])
    assert make_client(session).get("/v1/conversations").json() == {"items": [], "count": 0}
    sql = _sql(session.statements[0])
    assert "TOP 50" in sql and "ORDER BY conversations.updated_at DESC" in sql


@pytest.mark.parametrize("limit", [1, 25, 100])
def test_list_honours_the_limit(make_client: Any, limit: int) -> None:
    session = FakeSession([[]])
    assert make_client(session).get(f"/v1/conversations?limit={limit}").status_code == 200
    assert f"TOP {limit}" in _sql(session.statements[0])


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", "1.5", ""])
def test_list_rejects_a_limit_outside_1_100_with_422(make_client: Any, limit: str) -> None:
    session = FakeSession([[]])
    _assert_error(
        make_client(session).get(f"/v1/conversations?limit={limit}"), 422, "validation_error"
    )
    assert session.statements == []  # rejected before any query


# --- GET /v1/conversations/{id} -------------------------------------------------------------------


def test_open_returns_the_conversation_and_its_messages_oldest_first(make_client: Any) -> None:
    conversation = _conversation("Tracing")
    session = FakeSession(
        [
            [  # the DB returns newest first; the repository flips it
                _message(3, "assistant", "answer 2", [{"title": "T", "path": "docs/a.md"}]),
                _message(2, "user", "question 2"),
                _message(1, "assistant", "answer 1", None),
            ]
        ]
    )
    session.known[conversation.id] = conversation
    res = make_client(session).get(f"/v1/conversations/{conversation.id}")

    assert res.status_code == 200
    body = res.json()
    assert body["id"] == str(conversation.id) and body["title"] == "Tracing"
    assert body["truncated"] is False
    assert [m["content"] for m in body["messages"]] == ["answer 1", "question 2", "answer 2"]
    assert set(body["messages"][0]) == {"id", "role", "content", "citations", "created_at"}
    assert body["messages"][0]["citations"] is None
    assert body["messages"][2]["citations"] == [{"title": "T", "path": "docs/a.md"}]
    assert body["messages"][0]["created_at"] == "2026-10-06T10:00:00.123000Z"
    assert "ORDER BY messages.seq DESC" in _sql(session.statements[0])


def test_open_an_empty_conversation(make_client: Any) -> None:
    conversation = _conversation()
    session = FakeSession([[]])
    session.known[conversation.id] = conversation
    body = make_client(session).get(f"/v1/conversations/{conversation.id}").json()
    assert body["messages"] == [] and body["truncated"] is False


def test_open_bounds_the_thread_and_says_so(
    make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long conversation returns only the most recent messages, flagged ``truncated``."""
    conversation = _conversation()
    cap = 3
    monkeypatch.setattr(repo, "MAX_THREAD_MESSAGES", cap)  # the router reads it at call time
    newest_first = [_message(seq, "user", f"m{seq}") for seq in (5, 4, 3, 2)]  # cap + 1 rows
    session = FakeSession([newest_first])
    session.known[conversation.id] = conversation
    body = make_client(session).get(f"/v1/conversations/{conversation.id}").json()

    assert body["truncated"] is True
    assert [m["content"] for m in body["messages"]] == ["m3", "m4", "m5"]  # newest 3, oldest first
    assert f"TOP {cap + 1}" in _sql(session.statements[0])


def test_open_uses_the_real_cap_by_default(make_client: Any) -> None:
    """No patching: the router asks for the real limit (500) plus one row."""
    conversation = _conversation()
    session = FakeSession([[]])
    session.known[conversation.id] = conversation
    make_client(session).get(f"/v1/conversations/{conversation.id}")
    assert f"TOP {repo.MAX_THREAD_MESSAGES + 1}" in _sql(session.statements[0])
    assert repo.MAX_THREAD_MESSAGES == 500


def test_a_stored_citations_value_of_the_wrong_shape_is_a_clean_500(make_client: Any) -> None:
    """Valid JSON but not ``[{title, path}]``: the thread cannot be shaped; nothing leaks."""
    conversation = _conversation()
    bad = _message(1, "assistant", SECRET_MESSAGE, [{"title": "only a title"}])
    session = FakeSession([[bad]])
    session.known[conversation.id] = conversation
    res = make_client(session).get(f"/v1/conversations/{conversation.id}")
    _assert_error(res, 500, "internal_error")
    assert SECRET_MESSAGE not in res.text and "only a title" not in res.text


def test_open_unknown_conversation_is_404(make_client: Any) -> None:
    res = make_client(FakeSession()).get(f"/v1/conversations/{uuid4()}")
    _assert_error(res, 404, "not_found")


def test_open_with_a_malformed_id_is_422_not_a_query(make_client: Any) -> None:
    session = FakeSession()
    _assert_error(make_client(session).get("/v1/conversations/not-a-uuid"), 422, "validation_error")
    assert session.statements == []


# --- trace id, versioning, errors ----------------------------------------------------------------


def test_every_response_echoes_a_valid_inbound_trace_id(make_client: Any) -> None:
    client = make_client(FakeSession([[]]))
    ok = client.get("/v1/conversations", headers={"x-trace-id": INBOUND})
    bad = client.get("/v1/conversations?limit=0", headers={"x-trace-id": INBOUND})
    missing = client.get(f"/v1/conversations/{uuid4()}", headers={"x-trace-id": INBOUND})
    for res in (ok, bad, missing):
        assert res.headers["x-trace-id"] == INBOUND
    assert bad.json()["trace_id"] == INBOUND and missing.json()["trace_id"] == INBOUND


def test_a_database_failure_is_a_500_with_no_driver_text(make_client: Any) -> None:
    class Failing(FakeSession):
        async def execute(self, statement: Any) -> Any:
            raise RuntimeError(f"driver says: {SECRET_MESSAGE}")

    res = make_client(Failing()).get("/v1/conversations")
    _assert_error(res, 500, "internal_error")
    assert SECRET_MESSAGE not in res.text


def test_the_routes_exist_only_under_v1(make_client: Any) -> None:
    client = make_client(FakeSession())
    assert client.get("/conversations").status_code == 404
    assert client.post("/conversations").status_code in (404, 405)
    assert client.get("/v1/conversations").status_code == 200


# --- telemetry: no ids, titles or message text ---------------------------------------


def test_logs_carry_no_ids_titles_or_message_text(make_client: Any) -> None:
    conversation = _conversation(SECRET_TITLE)
    session = FakeSession([[conversation], [_message(1, "user", SECRET_MESSAGE)]])
    session.known[conversation.id] = conversation
    client = make_client(session)
    with structlog.testing.capture_logs() as logs:
        client.post("/v1/conversations", json={"title": SECRET_TITLE})
        client.get("/v1/conversations")
        client.get(f"/v1/conversations/{conversation.id}")
        client.get(f"/v1/conversations/{uuid4()}")  # a 404
        client.post("/v1/conversations", json={"title": "x" * 201 + SECRET_TITLE})  # a 422
    events = {entry["event"] for entry in logs}
    # The test must see the endpoints' own lines, or its "nothing leaked" claim is vacuous.
    assert {"conversation created", "conversations listed", "conversation opened"} <= events
    rendered = repr(logs)
    for forbidden in (SECRET_TITLE, SECRET_MESSAGE, str(conversation.id)):
        assert forbidden not in rendered


def test_metrics_use_the_route_pattern_never_the_id(
    make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = tracing_mod.record_server_duration

    def spy(route_class: str, method: str, status_code: int, duration_ms: float) -> None:
        seen.append(route_class)
        real(route_class, method, status_code, duration_ms)

    monkeypatch.setattr(tracing_mod, "record_server_duration", spy)
    conversation = _conversation()
    session = FakeSession([[]])
    session.known[conversation.id] = conversation
    make_client(session).get(f"/v1/conversations/{conversation.id}")
    assert "/v1/conversations/{conversation_id}" in seen
    assert not any(str(conversation.id) in route for route in seen)


# --- the contract --------------------------------------------------------------------------------


def test_openapi_documents_the_three_routes_and_their_errors() -> None:
    spec = app.openapi()
    assert set(spec["paths"]["/v1/conversations"]) == {"get", "post"}
    assert "201" in spec["paths"]["/v1/conversations"]["post"]["responses"]
    detail = spec["paths"]["/v1/conversations/{conversation_id}"]["get"]["responses"]
    assert {"200", "404", "422"} <= set(detail)
