"""SQL Server's own error text must not reach database spans (roadmap api 1.4).

``hide_parameters=True`` (api 1.3) keeps bound values out of SQLAlchemy's exception text, but the
OTel SQLAlchemy instrumentor has TWO paths that export the driver's raw message:

* a statement fails -> ``engine._handle_error`` sets the DB span's status description to
  ``str(context.original_exception)``;
* a connection fails (login rejected, firewall, unreachable host) -> the ``connect`` span from
  ``_wrap_connect`` records the exception event AND sets the status description.

The driver's message can quote user content (a truncation error carries ``Truncated value: '…'``).
``hide_driver_error_text`` swaps both for versions that report only the exception *kind*.

Offline: failures go through the real engine error paths with a fake connection, and spans are
read from an in-memory exporter. Nothing connects (rule 40).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import opentelemetry.instrumentation.sqlalchemy as sa_package
import pyodbc
import pytest
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.sqlalchemy import engine as sa_engine
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncEngine

import app.db.engine as engine_mod
import app.observability as obs

SECRET = "TOP-SECRET-USER-TEXT"
TEST_URL = (
    "mssql+aioodbc://u:p@db.invalid:1433/app"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
)


class _DriverError(pyodbc.Error):
    """A pyodbc error whose MESSAGE quotes content, as SQL Server's own errors can."""


class _Log:
    """Collects warnings the way ``get_logger(...)`` objects would receive them."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, message: str, **kwargs: Any) -> None:
        self.warnings.append((message, kwargs))


@dataclass
class Harness:
    exporter: InMemorySpanExporter
    install: Callable[..., None]


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    """Instrument SQLAlchemy into a private in-memory exporter; undo everything afterwards.

    ``install(hide=True)`` applies the swap first, as ``_instrument_sqlalchemy`` does;
    ``hide=False`` leaves upstream untouched — the control.
    """
    attempts: list[Any] = []
    monkeypatch.setattr(
        pyodbc, "connect", lambda *a, **k: attempts.append(a) or pytest.fail("must not connect")
    )
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    # Remember the upstream hooks so any swap is undone at teardown.
    monkeypatch.setattr(sa_engine, "_handle_error", sa_engine._handle_error)
    monkeypatch.setattr(sa_package, "_wrap_connect", sa_package._wrap_connect)
    SQLAlchemyInstrumentor().uninstrument()
    engine_mod._reset_for_tests()

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))

    def install(hide: bool = True) -> None:
        if hide:
            assert obs.hide_driver_error_text(_Log()) is True
        SQLAlchemyInstrumentor().instrument(tracer_provider=provider)

    yield Harness(memory, install)
    SQLAlchemyInstrumentor().uninstrument()
    engine_mod._reset_for_tests()
    assert attempts == []


def _prepare(engine: AsyncEngine) -> Any:
    sync_engine = engine.sync_engine
    sync_engine.pool._creator = lambda *args, **kwargs: MagicMock(name="raw-connection")
    dialect = sync_engine.dialect
    dialect.initialize = lambda connection: None
    dialect.on_connect = lambda: None
    dialect.do_ping = lambda dbapi_connection: True
    dialect.do_rollback = lambda dbapi_connection: None
    return sync_engine


def _fail_a_statement(engine: AsyncEngine) -> exc.DBAPIError:
    """A statement fails in the driver; the driver's message quotes SECRET."""
    sync_engine = _prepare(engine)

    def failing_execute(cursor: Any, statement: Any, parameters: Any, context: Any = None) -> None:
        raise _DriverError(f"String or binary data would be truncated. Truncated value: '{SECRET}'")

    sync_engine.dialect.do_execute = failing_execute

    async def run() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("INSERT INTO messages (content) VALUES (:c)"), {"c": "x"})

    with pytest.raises(exc.DBAPIError) as raised:
        asyncio.run(run())
    return raised.value


def _fail_to_connect(engine: AsyncEngine) -> exc.DBAPIError:
    """The login fails; the driver's message quotes SECRET (as a login name or server would)."""
    sync_engine = _prepare(engine)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise _DriverError(f"Login failed for user '{SECRET}'. Cannot open server.")

    sync_engine.pool._creator = refuse

    async def run() -> None:
        async with engine.connect():
            pass

    with pytest.raises(exc.DBAPIError) as raised:
        asyncio.run(run())
    return raised.value


def _span_named(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    spans = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(spans) == 1, [s.name for s in exporter.get_finished_spans()]
    return spans[0]


def _statement_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = [s for s in exporter.get_finished_spans() if "INSERT" in str(s.attributes)]
    assert len(spans) == 1, [s.name for s in exporter.get_finished_spans()]
    return spans[0]


def _everything_on(span: ReadableSpan) -> str:
    """All text a span would export: status, attributes and events (name + attributes)."""
    parts = [str(span.status.description), str(dict(span.attributes or {}))]
    parts += [f"{event.name} {dict(event.attributes or {})}" for event in span.events]
    return " | ".join(parts)


# --- a failing STATEMENT -----------------------------------------------------------------------


def test_control_upstream_puts_the_driver_message_on_the_statement_span(harness: Harness) -> None:
    """Proves the leak path is real, so the test below is not passing for the wrong reason."""
    harness.install(hide=False)
    _fail_a_statement(engine_mod.get_engine())
    span = _statement_span(harness.exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert SECRET in str(span.status.description)


def test_the_statement_span_carries_only_the_exception_kind(harness: Harness) -> None:
    harness.install()
    _fail_a_statement(engine_mod.get_engine())
    span = _statement_span(harness.exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "_DriverError"  # kind only: still diagnosable
    assert SECRET not in _everything_on(span)
    assert span.end_time is not None  # the span is still ended, as upstream does


# --- a failing CONNECTION (no execution context: _handle_error never runs) ----------------------


def test_control_upstream_puts_the_driver_message_on_the_connect_span(harness: Harness) -> None:
    harness.install(hide=False)
    _fail_to_connect(engine_mod.get_engine())
    span = _span_named(harness.exporter, "connect")
    assert SECRET in _everything_on(span)  # in the exception event and/or the status description
    assert any(event.name == "exception" for event in span.events)


def test_the_connect_span_carries_only_the_exception_kind(harness: Harness) -> None:
    harness.install()
    error = _fail_to_connect(engine_mod.get_engine())
    span = _span_named(harness.exporter, "connect")
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == type(error).__name__
    assert span.events == ()  # no exception event, so no message and no stacktrace
    assert SECRET not in _everything_on(span)
    assert span.attributes.get("net.peer.name") == "db.invalid"  # host/db attributes are kept


def test_a_successful_connect_is_unchanged(harness: Harness) -> None:
    harness.install()
    engine = engine_mod.get_engine()
    _prepare(engine)

    async def run() -> None:
        async with engine.connect():
            pass

    asyncio.run(run())
    span = _span_named(harness.exporter, "connect")
    assert span.status.status_code is not StatusCode.ERROR


# --- the swap itself ------------------------------------------------------------------------------


def test_the_swap_is_idempotent(harness: Harness) -> None:
    assert obs.hide_driver_error_text(_Log()) is True
    assert obs.hide_driver_error_text(_Log()) is True
    assert sa_engine._handle_error is obs._safe_handle_error
    assert sa_package._wrap_connect is obs._safe_wrap_connect


def test_the_swap_is_in_place_before_instrument_runs(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The package binds ``_wrap_connect`` when ``instrument()`` runs, so the order matters."""
    seen: dict[str, bool] = {}

    def spy(self: Any, **kwargs: Any) -> None:
        seen["handle_error"] = sa_engine._handle_error is obs._safe_handle_error
        seen["wrap_connect"] = sa_package._wrap_connect is obs._safe_wrap_connect

    monkeypatch.setattr(SQLAlchemyInstrumentor, "instrument", spy)
    obs._instrument_sqlalchemy(_Log())
    assert seen == {"handle_error": True, "wrap_connect": True}


def test_if_the_swap_is_impossible_no_db_spans_are_started(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: no DB spans rather than spans that export SQL Server's message."""
    started: list[bool] = []
    monkeypatch.setattr(
        SQLAlchemyInstrumentor, "instrument", lambda self, **kw: started.append(True)
    )
    monkeypatch.delattr(sa_engine, "_handle_error")
    log = _Log()
    obs._instrument_sqlalchemy(log)
    assert started == []
    messages = " ".join(message for message, _ in log.warnings)
    assert "could not hide driver error text" in messages
    assert "database spans disabled" in messages


# --- pin the upstream source we replace: a dependency bump that changes it must fail here -------


def _function_source(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


def test_upstream_still_has_the_two_paths_we_replace() -> None:
    """Reads the module SOURCE, so it is immune to test order and to an applied swap.

    If ``opentelemetry-instrumentation-sqlalchemy`` is bumped and one of these fails, revisit
    roadmap api 1.4: upstream may have fixed it (retire the swap) or moved it (adapt the swap).
    """
    engine_source = inspect.getsource(sa_engine)
    handler = _function_source(engine_source, "def _handle_error(context):", "def _get_attributes")
    assert "str(context.original_exception)" in handler
    assert "execution_context" in handler and "span.end()" in handler

    connect = _function_source(
        engine_source, "def _wrap_connect(tracer):", "return _wrap_connect_internal"
    )
    assert "start_as_current_span" in connect
    assert "record_exception" not in connect  # i.e. the tracer's default, True, applies

    package_source = inspect.getsource(sa_package)
    assert '"Engine.connect"' in package_source and "_wrap_connect(tracer)" in package_source

    for name in obs._UPSTREAM_ENGINE_NAMES:
        assert hasattr(sa_engine, name), name
