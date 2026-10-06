"""Test isolation shared by every test module.

``app.main`` (and ``alembic/env.py``) load ``apps/api/.env`` into ``os.environ`` for local dev. A
developer's real file holds ``EXTERNAL_DATABASE_URL``, ``AZURE_*`` and telemetry settings that would
then leak into the whole pytest process and make results depend on the machine.

Guard the loader BEFORE any test module imports ``app.main`` (collection imports it at module
level): reading the REAL ``apps/api/.env`` becomes a no-op, while any other path — the temporary
files the loader's own tests create — loads normally. ``LOCAL_ENV_FILE`` itself is left untouched,
so the tests that pin its value still hold.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import app.config as config_mod

_REAL_LOCAL_ENV_FILE = config_mod.LOCAL_ENV_FILE
_real_load_dotenv = config_mod.load_dotenv


def _load_dotenv_except_the_real_file(dotenv_path: Any = None, *args: Any, **kwargs: Any) -> bool:
    if dotenv_path is not None and Path(dotenv_path) == _REAL_LOCAL_ENV_FILE:
        return False
    return _real_load_dotenv(dotenv_path, *args, **kwargs)


config_mod.load_dotenv = _load_dotenv_except_the_real_file


# --- OpenTelemetry SQLAlchemy hooks ------------------------------------------------------------
# ``app.observability.hide_driver_error_text`` rebinds two module attributes of the instrumentor
# (see tests/test_db_span_hygiene.py). Tests that run the real ``_instrument_sqlalchemy`` would
# leave them swapped for the rest of the session and make later tests order-dependent, so the
# ORIGINALS are captured once here and put back after every test.
import opentelemetry.instrumentation.sqlalchemy as _otel_sa_package  # noqa: E402
import pytest  # noqa: E402
from opentelemetry.instrumentation.sqlalchemy import engine as _otel_sa_engine  # noqa: E402

_ORIGINAL_HANDLE_ERROR = _otel_sa_engine._handle_error
_ORIGINAL_WRAP_CONNECT = _otel_sa_package._wrap_connect


@pytest.fixture(autouse=True)
def _restore_otel_sqlalchemy_hooks() -> Any:
    yield
    _otel_sa_engine._handle_error = _ORIGINAL_HANDLE_ERROR
    _otel_sa_package._wrap_connect = _ORIGINAL_WRAP_CONNECT
