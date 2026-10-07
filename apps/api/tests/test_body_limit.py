"""The ``/v1`` request-size cap (api 5.5, app/body_limit.py).

A mini app with the production plumbing (error contract, the cap, the trace middleware, in the
order ``create_app`` adds them) and routes that read a body in every way FastAPI offers; plus the
real app, to prove the cap is wired in and what it leaves alone. No database, no model, no network.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator

import pytest
import structlog.testing
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

import app.main as main_mod
from app.body_limit import BodyLimitMiddleware, BodyTooLarge
from app.config import DEFAULT_MAX_REQUEST_BODY_BYTES, get_settings, read_body_limit
from app.errors import UnhandledErrorMiddleware, install_error_handlers
from app.main import app as real_app
from app.tracing import TraceMiddleware

TRACE_RE = re.compile(r"^[0-9a-f]{32}$")
INBOUND = "0eb01" + "e" * 27
LIMIT = 1000


class Question(BaseModel):
    question: str


def _mini_app(limit: int = LIMIT) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(BodyLimitMiddleware, max_bytes=limit)
    app.add_middleware(TraceMiddleware)
    router = APIRouter(prefix="/v1")
    reached: list[str] = []
    app.state.reached = reached

    @router.post("/model")
    async def model(body: Question) -> dict:
        reached.append("model")
        return {"length": len(body.question)}

    @router.post("/raw")
    async def raw(request: Request) -> dict:
        reached.append("raw")
        return {"bytes": len(await request.body())}

    @router.post("/streamed")
    async def streamed(request: Request) -> dict:
        reached.append("streamed")
        total = 0
        async for part in request.stream():
            total += len(part)
        return {"bytes": total}

    @router.post("/ignores-the-body")
    async def ignores() -> dict:
        reached.append("ignores")
        return {"ok": True}

    @router.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    app.include_router(router)

    for operational in ("/ping", "/info", "/health"):
        app.add_api_route(operational, lambda: {"ok": True}, methods=["GET"])

    @app.post("/not-v1")
    async def not_v1(request: Request) -> dict:
        return {"bytes": len(await request.body())}

    return app


@pytest.fixture
def mini() -> Iterator[TestClient]:
    with TestClient(_mini_app()) as client:
        yield client


def _chunks(total: int, size: int = 100) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        piece = min(size, total - sent)
        sent += piece
        yield b"x" * piece


def _assert_413(res) -> None:
    assert res.status_code == 413
    body = res.json()
    assert body["error"] == "payload_too_large"
    assert TRACE_RE.match(body["trace_id"])
    assert res.headers["x-trace-id"] == body["trace_id"]
    assert set(body) == {"error", "trace_id"}


# ── a declared length ─────────────────────────────────────────────────────────────────────────


class TestDeclaredLength:
    def test_a_body_over_the_limit_is_refused_before_the_route_runs(self, mini: TestClient) -> None:
        res = mini.post("/v1/raw", content=b"x" * (LIMIT + 1))
        _assert_413(res)
        assert mini.app.state.reached == []

    def test_a_body_exactly_at_the_limit_goes_through(self, mini: TestClient) -> None:
        res = mini.post("/v1/raw", content=b"x" * LIMIT)
        assert res.status_code == 200 and res.json() == {"bytes": LIMIT}

    def test_a_json_body_for_a_model_route_is_refused_with_the_same_413(
        self, mini: TestClient
    ) -> None:
        big = '{"question": "' + "q" * (LIMIT * 5) + '"}'
        _assert_413(
            mini.post("/v1/model", content=big, headers={"content-type": "application/json"})
        )
        assert mini.app.state.reached == []

    def test_the_inbound_trace_id_is_echoed_on_the_413(self, mini: TestClient) -> None:
        res = mini.post("/v1/raw", content=b"x" * (LIMIT + 1), headers={"x-trace-id": INBOUND})
        assert res.status_code == 413
        assert res.headers["x-trace-id"] == INBOUND and res.json()["trace_id"] == INBOUND


# ── a body that declares no length ────────────────────────────────────────────────────────────


class TestStreamedBody:
    def test_a_chunked_upload_over_the_limit_is_cut_off(self, mini: TestClient) -> None:
        with structlog.testing.capture_logs() as logs:
            res = mini.post("/v1/raw", content=_chunks(LIMIT * 10))
        _assert_413(res)
        # ... by the streamed count: no Content-Length was declared (that would be "declared").
        assert [e["kind"] for e in logs if e["event"] == "request body refused"] == ["streamed"]

    def test_the_cut_off_happens_for_every_way_a_route_can_read_the_body(
        self, mini: TestClient
    ) -> None:
        for path in ("/v1/raw", "/v1/streamed"):
            _assert_413(mini.post(path, content=_chunks(LIMIT * 4)))
        res = mini.post(
            "/v1/model",
            content=_chunks(LIMIT * 4),
            headers={"content-type": "application/json"},
        )
        _assert_413(res)

    def test_a_chunked_upload_just_under_the_limit_goes_through(self, mini: TestClient) -> None:
        res = mini.post("/v1/streamed", content=_chunks(LIMIT))
        assert res.status_code == 200 and res.json() == {"bytes": LIMIT}

    def test_the_streamed_count_is_the_sum_of_the_chunks_not_each_one(
        self, mini: TestClient
    ) -> None:
        # Chunks of 150 bytes: each far under the limit, together over it (the test client may
        # deliver them as one message; TestRawAsgi feeds them one at a time).
        _assert_413(mini.post("/v1/raw", content=_chunks(1500, size=150)))

    def test_a_route_that_never_reads_the_body_is_not_affected_by_a_streamed_one(
        self, mini: TestClient
    ) -> None:
        res = mini.post("/v1/ignores-the-body", content=_chunks(LIMIT * 4))
        assert res.status_code == 200


class TestRawAsgi:
    """The middleware on its own, fed message by message, so the count is exercised exactly."""

    @staticmethod
    async def _run(messages: list[dict], limit: int = LIMIT) -> list[int]:
        """Feed ``messages`` through the cap to an app that reads them all; returns what it saw."""
        seen: list[int] = []

        async def inner(scope, receive, send):  # noqa: ANN001
            while True:
                message = await receive()
                seen.append(len(message.get("body", b"")))
                if not message.get("more_body", False):
                    return

        queue = iter(messages)

        async def receive() -> dict:
            return next(queue)

        async def send(message: dict) -> None:
            return None

        scope = {"type": "http", "path": "/v1/x", "method": "POST", "headers": []}
        await BodyLimitMiddleware(inner, max_bytes=limit)(scope, receive, send)
        return seen

    def test_many_small_messages_add_up(self) -> None:
        messages = [
            {"type": "http.request", "body": b"x" * 150, "more_body": True} for _ in range(9)
        ] + [{"type": "http.request", "body": b"x" * 150, "more_body": False}]
        with pytest.raises(BodyTooLarge):
            asyncio.run(self._run(messages))

    def test_the_same_messages_pass_under_a_larger_limit(self) -> None:
        messages = [
            {"type": "http.request", "body": b"x" * 150, "more_body": True} for _ in range(9)
        ] + [{"type": "http.request", "body": b"x" * 150, "more_body": False}]
        assert asyncio.run(self._run(messages, limit=1500)) == [150] * 10


class TestHowThePathAndTheHeaderAreRead:
    @staticmethod
    async def _call(scope: dict, limit: int = LIMIT) -> list[dict]:
        sent: list[dict] = []

        async def inner(scope, receive, send):  # noqa: ANN001
            await receive()

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            sent.append(message)

        await BodyLimitMiddleware(inner, max_bytes=limit)(scope, receive, send)
        return sent

    @staticmethod
    def _scope(path: str, headers: list[tuple[bytes, bytes]], root_path: str = "") -> dict:
        return {
            "type": "http",
            "method": "POST",
            "path": path,
            "root_path": root_path,
            "headers": headers,
        }

    def test_a_root_path_does_not_hide_a_v1_route(self) -> None:
        scope = self._scope(
            "/api/v1/conversations", [(b"content-length", str(LIMIT + 1).encode())], "/api"
        )
        sent = asyncio.run(self._call(scope))
        assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413

    def test_an_operational_path_under_a_root_path_is_still_exempt(self) -> None:
        scope = self._scope("/api/health", [(b"content-length", str(LIMIT * 9).encode())], "/api")
        assert asyncio.run(self._call(scope)) == []  # the inner app ran and sent nothing

    def test_a_repeated_content_length_counts_as_its_largest(self) -> None:
        scope = self._scope(
            "/v1/x",
            [
                (b"content-length", b"10"),
                (b"content-length", str(LIMIT + 1).encode()),
                (b"content-length", b"not-a-number"),
            ],
        )
        assert asyncio.run(self._call(scope))[0]["status"] == 413

    def test_a_garbage_content_length_alone_falls_through_to_the_streamed_count(self) -> None:
        scope = self._scope("/v1/x", [(b"content-length", b"not-a-number")])
        assert asyncio.run(self._call(scope)) == []

    def test_a_declared_413_says_the_connection_ends(self) -> None:
        scope = self._scope("/v1/x", [(b"content-length", str(LIMIT + 1).encode())])
        headers = dict(asyncio.run(self._call(scope))[0]["headers"])
        assert headers[b"connection"] == b"close"

    def test_a_websocket_scope_passes_through(self) -> None:
        called: list[str] = []

        async def inner(scope, receive, send):  # noqa: ANN001
            called.append(scope["type"])

        asyncio.run(
            BodyLimitMiddleware(inner, max_bytes=1)(
                {"type": "websocket", "path": "/v1/x"}, None, None
            )
        )
        assert called == ["websocket"]


# ── what it leaves alone ──────────────────────────────────────────────────────────────────────


class TestScope:
    def test_every_route_but_the_operational_ones_is_limited(self, mini: TestClient) -> None:
        _assert_413(mini.post("/not-v1", content=b"x" * (LIMIT * 5)))
        # the next API version, a path that merely looks like /v1, and an unknown path
        for path in ("/v2/anything", "/v10/anything", "/nowhere"):
            _assert_413(mini.post(path, content=b"x" * (LIMIT * 5)))

    def test_the_operational_routes_take_no_body_and_are_left_alone(self, mini: TestClient) -> None:
        for path in ("/ping", "/info", "/health"):
            assert mini.request("GET", path, content=b"x" * (LIMIT * 5)).status_code == 200

    def test_a_normal_request_is_unaffected(self, mini: TestClient) -> None:
        assert mini.get("/v1/ok").json() == {"ok": True}
        res = mini.post("/v1/model", json={"question": "Which port does the api use?"})
        assert res.status_code == 200 and res.json() == {"length": 28}

    def test_an_error_route_still_gets_the_contract_for_a_malformed_body(
        self, mini: TestClient
    ) -> None:
        res = mini.post("/v1/model", content=b"{nope", headers={"content-type": "application/json"})
        assert res.status_code == 422 and res.json()["error"] == "validation_error"


# ── logs ──────────────────────────────────────────────────────────────────────────────────────


class TestLogs:
    def test_a_refusal_logs_its_kind_only(self, mini: TestClient) -> None:
        with structlog.testing.capture_logs() as logs:
            mini.post("/v1/raw", content=b"SECRET-BODY-TEXT" * 100)
            mini.post("/v1/raw", content=_chunks(LIMIT * 3))
        refusals = [e for e in logs if e["event"] == "request body refused"]
        assert [e["kind"] for e in refusals] == ["declared", "streamed"]
        rendered = repr(logs)
        assert "SECRET-BODY-TEXT" not in rendered and "/v1/raw" not in rendered
        assert all(set(e) <= {"event", "kind", "log_level"} for e in refusals)


# ── the setting ───────────────────────────────────────────────────────────────────────────────


class TestSetting:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, DEFAULT_MAX_REQUEST_BODY_BYTES),
            ("", DEFAULT_MAX_REQUEST_BODY_BYTES),
            ("   ", DEFAULT_MAX_REQUEST_BODY_BYTES),
            ("2048", 2048),
            (" 2048 ", 2048),
            ("1", 1),
            ("0", DEFAULT_MAX_REQUEST_BODY_BYTES),
            ("-5", DEFAULT_MAX_REQUEST_BODY_BYTES),
            ("64KB", DEFAULT_MAX_REQUEST_BODY_BYTES),
            ("1e6", DEFAULT_MAX_REQUEST_BODY_BYTES),
            ("1048576", 1048576),
            ("1048577", DEFAULT_MAX_REQUEST_BODY_BYTES),
            ("99999999999", DEFAULT_MAX_REQUEST_BODY_BYTES),
        ],
    )
    def test_a_mistyped_value_is_the_default_never_no_cap(
        self, raw: str | None, expected: int
    ) -> None:
        assert read_body_limit(raw) == expected

    def test_the_default_is_64_kb(self) -> None:
        assert DEFAULT_MAX_REQUEST_BODY_BYTES == 64 * 1024

    def test_settings_read_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "4096")
        assert get_settings().max_request_body_bytes == 4096
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "nonsense")
        assert get_settings().max_request_body_bytes == DEFAULT_MAX_REQUEST_BODY_BYTES
        monkeypatch.delenv("MAX_REQUEST_BODY_BYTES")
        assert get_settings().max_request_body_bytes == DEFAULT_MAX_REQUEST_BODY_BYTES


# ── the real app ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def real() -> Iterator[TestClient]:
    with TestClient(real_app) as client:
        yield client


class TestRealApp:
    def test_the_cap_is_wired_into_the_app_between_the_error_net_and_the_trace_middleware(
        self,
    ) -> None:
        names = [m.cls.__name__ for m in real_app.user_middleware]
        assert names.index("TraceMiddleware") < names.index("BodyLimitMiddleware")
        assert names.index("BodyLimitMiddleware") < names.index("UnhandledErrorMiddleware")

    def test_a_big_question_to_a_real_route_is_refused_with_the_contract(
        self, real: TestClient
    ) -> None:
        big = '{"question": "' + "q" * (DEFAULT_MAX_REQUEST_BODY_BYTES + 10) + '"}'
        res = real.post(
            "/v1/conversations/5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01/ask",
            content=big,
            headers={"content-type": "application/json"},
        )
        _assert_413(res)

    def test_a_chunked_upload_to_the_stream_route_is_cut_off(self, real: TestClient) -> None:
        res = real.post(
            "/v1/conversations/5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01/ask/stream",
            content=_chunks(DEFAULT_MAX_REQUEST_BODY_BYTES * 2, size=8192),
            headers={"content-type": "application/json"},
        )
        _assert_413(res)

    def test_a_body_under_the_limit_reaches_the_route(self, real: TestClient) -> None:
        # No database is needed: the wrong type is rejected by validation, which is the route's own.
        res = real.post("/v1/conversations", json={"title": 5})
        assert res.status_code == 422 and res.json()["error"] == "validation_error"

    def test_the_operational_routes_are_unaffected(self, real: TestClient) -> None:
        for path in ("/ping", "/info", "/health"):
            res = real.request("GET", path, content=b"x" * (DEFAULT_MAX_REQUEST_BODY_BYTES * 2))
            assert res.status_code == 200

    def test_a_configured_limit_applies_to_a_new_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "50")
        small = main_mod.create_app()
        with TestClient(small) as client:
            _assert_413(client.post("/v1/conversations", json={"title": "x" * 60}))
            assert client.post("/v1/conversations", json={"title": 5}).status_code == 422

    def test_a_question_at_the_documented_maximum_fits(self) -> None:
        # 4,000 emoji, every one a JSON surrogate-pair escape (12 bytes): the worst legitimate body.
        worst = '{"question": "' + "\\ud83d\\ude00" * 4000 + '"}'
        assert len(worst.encode()) < DEFAULT_MAX_REQUEST_BODY_BYTES

    def test_body_too_large_is_a_413_http_exception(self) -> None:
        exc = BodyTooLarge()
        assert exc.status_code == 413 and exc.detail == "payload_too_large"
