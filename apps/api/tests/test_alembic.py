"""Alembic wiring (ADR-0004 / ADR-0008): no hard-coded URL, and the async ``env.py`` runs in
OFFLINE mode with no database and no connection — rendering for the SQL Server dialect.

The revision itself (``create conversations and messages``) is covered by
``tests/test_migration_conversations.py``.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

import pyodbc
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

SERVICE_ROOT = Path(__file__).resolve().parents[1]
INI = SERVICE_ROOT / "alembic.ini"
ENV_PY = SERVICE_ROOT / "alembic" / "env.py"
VERSIONS = SERVICE_ROOT / "alembic" / "versions"

TEST_URL = (
    "mssql+aioodbc://u:p@db.invalid:1433/app"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
)


def _config(output: io.StringIO | None = None) -> Config:
    return Config(str(INI), output_buffer=output, stdout=io.StringIO())


@pytest.fixture()
def _restore_logging():
    """env.py applies alembic.ini's CLI logging via fileConfig; undo it afterwards
    so the service's stdlib logging setup is untouched for the rest of the run."""
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    yield
    root.setLevel(saved[0])
    root.handlers[:] = saved[1]
    for name in ("alembic", "sqlalchemy.engine"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def test_ini_has_no_hardcoded_database_url() -> None:
    assert "sqlalchemy.url" not in INI.read_text(encoding="utf-8")


def test_script_location_resolves_to_the_alembic_dir() -> None:
    script = ScriptDirectory.from_config(_config())
    assert Path(script.dir).resolve() == (SERVICE_ROOT / "alembic").resolve()
    assert (SERVICE_ROOT / "alembic" / "script.py.mako").is_file()


def test_versions_dir_holds_a_single_linear_history() -> None:
    assert VERSIONS.is_dir()
    script = ScriptDirectory.from_config(_config())
    # One head: a second head means two branches were created and nobody merged them.
    assert len(script.get_heads()) == 1
    revisions = list(script.walk_revisions())
    files = [p for p in VERSIONS.glob("*.py") if p.name != "__init__.py"]
    assert len(revisions) == len(files)


def test_env_py_reads_url_from_settings_and_targets_base_metadata() -> None:
    source = ENV_PY.read_text(encoding="utf-8")
    compile(source, str(ENV_PY), "exec")  # syntactically valid
    assert "get_settings().database_url" in source
    assert "target_metadata = Base.metadata" in source
    assert "import app.models" in source
    assert 'config.get_main_option("sqlalchemy.url")' not in source


@pytest.mark.usefixtures("_restore_logging")
def test_offline_upgrade_runs_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[tuple[Any, ...]] = []

    def fake_connect(*args: Any, **kwargs: Any) -> None:
        attempts.append((args, kwargs))
        raise AssertionError("offline mode must never connect")

    monkeypatch.setattr(pyodbc, "connect", fake_connect)
    monkeypatch.setenv("DATABASE_URL", TEST_URL)

    output = io.StringIO()
    command.upgrade(_config(output), "head", sql=True)  # `alembic upgrade head --sql`

    assert attempts == []
    # The revision's DDL is asserted in test_migration_conversations.py; here only that it
    # rendered T-SQL (wrapped in a transaction) without a connection.
    sql = output.getvalue()
    assert "BEGIN TRANSACTION" in sql and "COMMIT" in sql
    assert "CREATE TABLE" in sql  # the revisions' DDL, asserted per revision elsewhere
