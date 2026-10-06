"""Alembic reads ``apps/api/.env`` the way the service does (roadmap api 1.5).

Only ``app.main`` used to call ``load_local_env()``, so ``alembic check`` / ``upgrade`` from a plain
terminal silently fell back to the template's placeholder URL even when ``apps/api/.env`` held a
real one. These tests run Alembic OFFLINE with a temporary ``.env``; nothing connects and no real
file or credential is read (rule 40).
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyodbc
import pytest
from alembic import command
from alembic.config import Config

import app.config as config_mod

SERVICE_ROOT = Path(__file__).resolve().parents[1]
INI = SERVICE_ROOT / "alembic.ini"
ENV_PY = SERVICE_ROOT / "alembic" / "env.py"

PASSWORD = "S3CR3T-PW-DO-NOT-PRINT"
URL_FROM_FILE = (
    f"mssql+aioodbc://dev:{PASSWORD}@from-file.invalid:1433/app"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
)
URL_FROM_SHELL = (
    "mssql+aioodbc://u:p@from-shell.invalid:1433/app"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Never connect; undo the logging env.py configures."""
    attempts: list[Any] = []

    def fake_connect(*args: Any, **kwargs: Any) -> None:
        attempts.append(args)
        raise AssertionError("offline mode must never connect")

    monkeypatch.setattr(pyodbc, "connect", fake_connect)
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    yield attempts
    assert attempts == []
    root.setLevel(saved[0])
    root.handlers[:] = saved[1]
    for name in ("alembic", "sqlalchemy.engine"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def _env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None) -> None:
    """Point the service's local env file at a temporary one (or at a missing one)."""
    target = tmp_path / ".env"
    if content is not None:
        target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config_mod, "LOCAL_ENV_FILE", target)


def _no_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start without ``DATABASE_URL``; set-then-delete so the test's teardown removes anything
    ``load_dotenv`` puts into ``os.environ`` afterwards."""
    monkeypatch.setenv("DATABASE_URL", "placeholder")
    monkeypatch.delenv("DATABASE_URL")


def _run_offline_upgrade() -> str:
    output = io.StringIO()
    command.upgrade(Config(str(INI), output_buffer=output, stdout=io.StringIO()), "head", sql=True)
    return output.getvalue()


def _spy_on_resolved_url(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the ``database_url`` that ``alembic/env.py`` reads from ``get_settings()``.

    ``env.py`` runs afresh for every Alembic command and does ``from app.config import
    get_settings``, so patching the attribute on ``app.config`` is what it picks up.
    """
    seen: list[str] = []
    real = config_mod.get_settings

    def spy() -> Any:
        settings = real()
        seen.append(settings.database_url)
        return settings

    monkeypatch.setattr(config_mod, "get_settings", spy)
    return seen


def test_env_py_loads_the_local_env_before_reading_settings() -> None:
    source = ENV_PY.read_text(encoding="utf-8")
    body = source[source.index("def _database_url") :].split("\ndef ", 2)[0]
    assert "load_local_env()" in body
    assert body.index("load_local_env()") < body.index("get_settings().database_url")


def test_the_env_file_is_loaded_when_the_shell_has_no_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_database_url(monkeypatch)
    _env_file(tmp_path, monkeypatch, f"DATABASE_URL={URL_FROM_FILE}\n")
    seen = _spy_on_resolved_url(monkeypatch)
    _run_offline_upgrade()
    assert os.environ["DATABASE_URL"] == URL_FROM_FILE
    assert seen == [URL_FROM_FILE]  # the URL Alembic's env.py actually resolved


def test_a_database_url_already_in_the_shell_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", URL_FROM_SHELL)
    _env_file(tmp_path, monkeypatch, f"DATABASE_URL={URL_FROM_FILE}\n")
    seen = _spy_on_resolved_url(monkeypatch)
    _run_offline_upgrade()
    assert os.environ["DATABASE_URL"] == URL_FROM_SHELL
    assert seen == [URL_FROM_SHELL]


def test_a_missing_env_file_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL_FROM_SHELL)
    _env_file(tmp_path, monkeypatch, None)
    sql = _run_offline_upgrade()
    assert "BEGIN TRANSACTION" in sql
    assert os.environ["DATABASE_URL"] == URL_FROM_SHELL


def test_the_url_is_never_printed_or_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    _no_database_url(monkeypatch)
    _env_file(tmp_path, monkeypatch, f"DATABASE_URL={URL_FROM_FILE}\n")
    sql = _run_offline_upgrade()
    captured = capfd.readouterr()
    for text in (sql, captured.out, captured.err):
        assert PASSWORD not in text
        assert "from-file.invalid" not in text
