"""Revision ``create ingest locks`` (roadmap api 5.2).

Everything runs in Alembic OFFLINE mode: the SQL Server DDL is rendered, never executed, and
``pyodbc.connect`` must never be called (rule 40). The parity test is the offline stand-in for
``alembic check``: the revision and the model must produce the same DDL.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

import pyodbc
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mssql
from sqlalchemy.schema import CreateTable

import app.models  # noqa: F401  — registers the models on Base.metadata
from app.db import Base
from app.models.ingest_lock import LOCK_NAME_MAX_LENGTH, IngestLock

SERVICE_ROOT = Path(__file__).resolve().parents[1]
INI = SERVICE_ROOT / "alembic.ini"
REVISION = "9c3e5a7d1f42"
PARENT = "5228ef6b6604"

TEST_URL = (
    "mssql+aioodbc://u:p@db.invalid:1433/app"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch):
    attempts: list[tuple[Any, ...]] = []

    def fake_connect(*args: Any, **kwargs: Any) -> None:
        attempts.append((args, kwargs))
        raise AssertionError("offline mode must never connect")

    monkeypatch.setattr(pyodbc, "connect", fake_connect)
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    yield
    assert attempts == []
    root.setLevel(saved[0])
    root.handlers[:] = saved[1]
    for name in ("alembic", "sqlalchemy.engine"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def _config(output: io.StringIO) -> Config:
    return Config(str(INI), output_buffer=output, stdout=io.StringIO())


def _upgrade_sql() -> str:
    """Only this revision's upgrade (``PARENT:REVISION``) — the first revision is not repeated."""
    output = io.StringIO()
    command.upgrade(_config(output), f"{PARENT}:{REVISION}", sql=True)
    return output.getvalue()


def _downgrade_sql() -> str:
    output = io.StringIO()
    command.downgrade(_config(output), f"{REVISION}:{PARENT}", sql=True)
    return output.getvalue()


def _lines(ddl: str) -> list[str]:
    return sorted(line.strip().rstrip(",;") for line in ddl.splitlines() if line.strip())


def _table_block(sql: str, name: str) -> str:
    block: list[str] = []
    inside = False
    for line in sql.splitlines():
        if line.startswith(f"CREATE TABLE {name} ("):
            inside = True
        elif inside and line.strip() == "GO":
            break
        if inside:
            block.append(line)
    assert block, f"CREATE TABLE {name} not found"
    return "\n".join(block)


def test_the_revision_follows_the_first_one_and_the_chain_has_one_head() -> None:
    script = ScriptDirectory.from_config(_config(io.StringIO()))
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision == PARENT
    assert len(script.get_heads()) == 1  # a later revision may move the head; two heads may not


def test_upgrade_creates_the_one_table_and_records_the_version() -> None:
    sql = _upgrade_sql()
    assert "CREATE TABLE ingest_locks" in sql
    assert "CREATE TABLE conversations" not in sql  # the first revision is not repeated
    assert f"UPDATE alembic_version SET version_num='{REVISION}'" in sql
    assert f"WHERE alembic_version.version_num = '{PARENT}'" in sql


def test_upgrade_uses_sql_server_safe_types() -> None:
    block = _table_block(_upgrade_sql(), "ingest_locks")
    assert f"name NVARCHAR({LOCK_NAME_MAX_LENGTH}) NOT NULL" in block
    assert "holder UNIQUEIDENTIFIER NOT NULL" in block
    assert "acquired_at DATETIME2(3) NOT NULL" in block
    assert "expires_at DATETIME2(3) NOT NULL" in block
    assert "CONSTRAINT pk_ingest_locks PRIMARY KEY (name)" in block
    assert "NTEXT" not in block


def test_the_migration_and_the_model_produce_the_same_ddl() -> None:
    """Offline drift check: every model column/constraint line is in the revision and vice versa."""
    dialect = mssql.dialect()
    table = IngestLock.__table__
    wrapper = {f"CREATE TABLE {table.name} (", ")"}
    expected = [
        x for x in _lines(str(CreateTable(table).compile(dialect=dialect))) if x not in wrapper
    ]
    actual = [x for x in _lines(_table_block(_upgrade_sql(), table.name)) if x not in wrapper]
    assert actual == expected


def test_every_model_table_is_created_by_the_whole_chain() -> None:
    output = io.StringIO()
    command.upgrade(_config(output), "head", sql=True)
    created = set(re.findall(r"^CREATE TABLE (\w+) \(", output.getvalue(), flags=re.MULTILINE))
    assert created - {"alembic_version"} == set(Base.metadata.tables)


def test_the_lock_name_is_the_primary_key_so_a_second_row_is_refused() -> None:
    assert [c.name for c in IngestLock.__table__.primary_key.columns] == ["name"]


def test_downgrade_drops_the_table_and_steps_the_version_back() -> None:
    sql = _downgrade_sql()
    assert "DROP TABLE ingest_locks" in sql
    assert "DROP TABLE conversations" not in sql and "DROP TABLE messages" not in sql
    assert f"UPDATE alembic_version SET version_num='{PARENT}'" in sql


def test_downgrade_mirrors_upgrade() -> None:
    assert "CREATE TABLE ingest_locks" in _upgrade_sql()
    assert "DROP TABLE ingest_locks" in _downgrade_sql()


def test_the_lock_holds_no_content() -> None:
    names = {c.name for c in IngestLock.__table__.columns}
    assert names == {"name", "holder", "acquired_at", "expires_at"}
