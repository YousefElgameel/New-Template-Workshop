"""Resource ids must not reach the request span (roadmap api 2.1).

The FastAPI/ASGI instrumentation records the CONCRETE request target on the server span
(``http.target``, ``http.url``; ``url.path``/``url.full`` under the stable conventions), so
``GET /v1/conversations/<uuid>`` would put the conversation id into Application Insights even
though ``http.route`` and every metric use the pattern. ``scrub_ids_server_hook`` rewrites any
UUID in those attributes. A control run without the hook proves the leak is real.

Offline: a throw-away FastAPI app, an in-memory exporter, no network (rule 40).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.observability as obs


class _Log:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, message: str, **kwargs: Any) -> None:
        self.warnings.append((message, kwargs))


def _request(with_hook: bool, path: str) -> list[ReadableSpan]:
    """One request through a throw-away app instrumented into a private in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = FastAPI()

    @app.get("/v1/conversations/{conversation_id}")
    async def one(conversation_id: str) -> dict[str, str]:
        return {"ok": "yes"}

    kwargs: dict[str, Any] = {"tracer_provider": provider}
    if with_hook:
        kwargs["server_request_hook"] = obs.scrub_ids_server_hook
    FastAPIInstrumentor.instrument_app(app, **kwargs)
    try:
        assert TestClient(app).get(path).status_code == 200
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
    return list(exporter.get_finished_spans())


def _everything(spans: list[ReadableSpan]) -> str:
    return " | ".join(f"{s.name} {dict(s.attributes or {})}" for s in spans)


def test_control_the_request_span_carries_the_id_without_the_hook() -> None:
    """If this ever stops failing-to-hide, the instrumentation changed — revisit the hook."""
    conversation_id = str(uuid4())
    spans = _request(with_hook=False, path=f"/v1/conversations/{conversation_id}")
    assert conversation_id in _everything(spans)


def test_the_hook_keeps_the_id_off_every_span_attribute_and_name() -> None:
    conversation_id = str(uuid4())
    spans = _request(with_hook=True, path=f"/v1/conversations/{conversation_id}")
    assert spans, "the request produced no span"
    assert conversation_id not in _everything(spans)
    server = next(s for s in spans if (s.attributes or {}).get("http.method") == "GET")
    assert server.attributes["http.target"] == "/v1/conversations/{id}"
    assert server.attributes["http.route"] == "/v1/conversations/{conversation_id}"  # the pattern


def test_the_hook_covers_the_full_url_and_an_uppercase_id() -> None:
    conversation_id = str(uuid4()).upper()
    spans = _request(with_hook=True, path=f"/v1/conversations/{conversation_id}")
    text = _everything(spans)
    assert conversation_id not in text and conversation_id.lower() not in text


def test_a_path_without_an_id_is_left_alone() -> None:
    spans = _request(with_hook=True, path="/v1/conversations/not-a-uuid-at-all")
    server = next(s for s in spans if (s.attributes or {}).get("http.method") == "GET")
    assert server.attributes["http.target"] == "/v1/conversations/not-a-uuid-at-all"


def test_the_hook_never_raises() -> None:
    class Broken:
        def is_recording(self) -> bool:
            raise RuntimeError("boom")

    obs.scrub_ids_server_hook(Broken(), {})  # must swallow it
    obs.scrub_ids_server_hook(object(), {})  # not even a span


def test_the_service_wires_the_hook_into_its_fastapi_instrumentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_instrument_app(app: Any, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", staticmethod(fake_instrument_app))
    obs._instrument_fastapi(object(), _Log())
    assert seen.get("server_request_hook") is obs.scrub_ids_server_hook
