"""Database errors must not carry user content (roadmap api 1.3).

Bound parameters are message text, titles and citations. SQLAlchemy puts them in
``str(exc)`` (``[parameters: (...)]``) unless the engine is created with
``hide_parameters=True``, and the OTel instrumentation copies ``str(exc)`` and the traceback
onto the span as ``exception.message`` / ``exception.stacktrace``.

Offline: no connection is ever opened and no database exists (rule 40).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pyodbc
import pytest
import structlog.testing
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncEngine

import app.db.engine as engine_mod
import app.db.external as external_mod
from app.errors import UnhandledErrorMiddleware, install_error_handlers
from app.tracing import TraceMiddleware

SECRET = "TOP-SECRET-MESSAGE-TEXT"
STATEMENT = "INSERT INTO messages (content) VALUES (?)"

TEST_URL = (
    "mssql+aioodbc://u:p@db.invalid:1433/app"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
)


class _DriverError(pyodbc.Error):
    """Stands in for the pyodbc error that SQL Server would raise."""


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    attempts: list[Any] = []

    def fake_connect(*args: Any, **kwargs: Any) -> None:
        attempts.append(args)
        raise AssertionError("must never connect")

    monkeypatch.setattr(pyodbc, "connect", fake_connect)
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("EXTERNAL_DATABASE_URL", TEST_URL)
    engine_mod._reset_for_tests()
    external_mod._reset_for_tests()
    yield attempts
    engine_mod._reset_for_tests()
    external_mod._reset_for_tests()
    assert attempts == []


def _db_error(*, hide_parameters: bool) -> exc.DBAPIError:
    error = exc.DBAPIError.instance(
        STATEMENT,
        (SECRET,),
        _DriverError("constraint failed"),
        Exception,
        hide_parameters=hide_parameters,
    )
    assert isinstance(error, exc.DBAPIError)
    return error


# --- the engines are created with the setting ---------------------------------------------------


def test_project_engine_hides_parameters() -> None:
    assert engine_mod.get_engine().sync_engine.hide_parameters is True


def test_external_engine_hides_parameters() -> None:
    assert external_mod.get_external_engine().sync_engine.hide_parameters is True


def _fail_a_statement(
    engine: AsyncEngine, statement: str = "INSERT INTO messages (content) VALUES (:content)"
) -> exc.DBAPIError:
    """Run an INSERT through the engine's OWN error path and return the wrapped exception.

    Nothing connects: the pool hands out a fake raw connection and the dialect's ``do_execute``
    raises a driver error, so SQLAlchemy builds the ``DBAPIError`` exactly as it would for a real
    constraint violation — using the engine's ``hide_parameters`` setting.
    """
    sync_engine = engine.sync_engine
    sync_engine.pool._creator = lambda *args, **kwargs: MagicMock(name="raw-connection")
    dialect = sync_engine.dialect
    dialect.initialize = lambda connection: None
    dialect.on_connect = lambda: None
    dialect.do_ping = lambda dbapi_connection: True
    dialect.do_rollback = lambda dbapi_connection: None

    def failing_execute(cursor: Any, statement: Any, parameters: Any, context: Any = None) -> None:
        raise _DriverError("constraint failed")

    dialect.do_execute = failing_execute

    async def run() -> None:
        async with engine.connect() as connection:
            await connection.execute(text(statement), {"content": SECRET})

    with pytest.raises(exc.DBAPIError) as raised:
        asyncio.run(run())
    return raised.value


def test_a_failing_statement_on_the_project_engine_carries_no_values() -> None:
    error = _fail_a_statement(engine_mod.get_engine())
    assert SECRET not in str(error) and SECRET not in repr(error)
    assert "INSERT INTO messages" in str(error)  # the statement text (placeholders) stays


def test_a_failing_statement_on_the_external_engine_carries_no_values() -> None:
    # The external engine is read-only (its guard refuses writes), so the statement is a SELECT.
    error = _fail_a_statement(
        external_mod.get_external_engine(), "SELECT * FROM orders WHERE note = :content"
    )
    assert SECRET not in str(error) and SECRET not in repr(error)


def test_control_the_same_failing_statement_leaks_when_the_setting_is_off() -> None:
    """Proves the two tests above exercise the real path and would catch a regression."""
    engine = engine_mod.get_engine()
    engine.sync_engine.hide_parameters = False
    assert SECRET in str(_fail_a_statement(engine))


# --- why it matters: the setting is what keeps the values out ----------------------------------


def test_without_the_setting_the_values_are_in_the_exception_text() -> None:
    """The control case: proves the next test is not passing for the wrong reason."""
    assert SECRET in str(_db_error(hide_parameters=False))


def test_with_the_engine_setting_the_values_are_not_in_the_exception_text() -> None:
    hide = engine_mod.get_engine().sync_engine.hide_parameters
    error = _db_error(hide_parameters=hide)
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert "hidden" in str(error)  # SQLAlchemy says the parameters were withheld


def test_the_span_event_for_a_db_error_carries_no_values() -> None:
    """OTel's ``record_exception`` copies ``str(exc)`` and the traceback onto the span."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    def recorded_text(hide_parameters: bool) -> str:
        exporter.clear()
        error = _db_error(hide_parameters=hide_parameters)
        try:
            raise error
        except exc.DBAPIError as caught:
            with tracer.start_as_current_span("db"):
                trace.get_current_span().record_exception(caught)
        (span,) = exporter.get_finished_spans()
        (event,) = span.events
        return " ".join(str(v) for v in dict(event.attributes or {}).values())

    assert SECRET in recorded_text(hide_parameters=False)  # control: the leak path is real
    assert SECRET not in recorded_text(
        hide_parameters=engine_mod.get_engine().sync_engine.hide_parameters
    )


# --- the api's error handling never echoes it either ------------------------------------------


def _app_raising(error: BaseException) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(TraceMiddleware)
    router = APIRouter(prefix="/v1")

    @router.get("/db-fails")
    async def db_fails() -> dict:
        raise error

    app.include_router(router)
    return app


@pytest.mark.parametrize("hide_parameters", [True, False])
def test_a_database_error_reaches_neither_the_response_nor_the_logs(
    hide_parameters: bool,
) -> None:
    """Defence in depth: the handlers log only the exception kind and place, whatever the flag."""
    error = _db_error(hide_parameters=hide_parameters)
    client = TestClient(_app_raising(error), raise_server_exceptions=False)
    with structlog.testing.capture_logs() as logs:
        res = client.get("/v1/db-fails")

    assert res.status_code == 500
    body = res.json()
    assert set(body) == {"error", "trace_id"} and body["error"] == "internal_error"
    assert SECRET not in res.text
    assert SECRET not in repr(logs)
    assert any(entry.get("kind") == "DBAPIError" for entry in logs), logs
