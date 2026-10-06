"""Revision ``create conversations and messages`` (roadmap api 1.2).

Everything runs in Alembic OFFLINE mode: the SQL Server DDL is rendered, never executed, and
``pyodbc.connect`` must never be called (rule 40). The parity test is the offline stand-in for
``alembic check``: the revision and the models must produce the same DDL.
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
from sqlalchemy.dialects import mssql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  — registers the models on Base.metadata
from app.db import Base

SERVICE_ROOT = Path(__file__).resolve().parents[1]
INI = SERVICE_ROOT / "alembic.ini"
REVISION = "5228ef6b6604"

TEST_URL = (
    "mssql+aioodbc://u:p@db.invalid:1433/app"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch):
    """No connection, a dummy URL, and env.py's CLI logging undone afterwards."""
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
    """This revision's upgrade only (``base:REVISION``) — a later revision must not leak in."""
    output = io.StringIO()
    command.upgrade(_config(output), f"base:{REVISION}", sql=True)
    return output.getvalue()


def _downgrade_sql() -> str:
    output = io.StringIO()
    command.downgrade(_config(output), f"{REVISION}:base", sql=True)
    return output.getvalue()


def _lines(ddl: str) -> list[str]:
    """Normalised, order-independent lines of a DDL script (column and constraint lines).

    A sorted list, not a set, so a duplicated line is not masked.
    """
    return sorted(line.strip().rstrip(",;") for line in ddl.splitlines() if line.strip())


def _table_block(sql: str, name: str) -> str:
    """The ``CREATE TABLE name (...)`` statement, cut at the next ``GO`` batch-separator line."""
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


def test_the_revision_is_the_first_migration() -> None:
    script = ScriptDirectory.from_config(_config(io.StringIO()))
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision is None


def test_upgrade_creates_both_tables_parent_first() -> None:
    sql = _upgrade_sql()
    assert sql.index("CREATE TABLE conversations") < sql.index("CREATE TABLE messages")
    assert "INSERT INTO alembic_version (version_num)" in sql
    assert f"VALUES ('{REVISION}')" in sql


def test_upgrade_uses_sql_server_safe_types_and_constraints() -> None:
    sql = _upgrade_sql()
    assert "seq BIGINT NOT NULL IDENTITY(1,1)" in sql
    assert "content NVARCHAR(max) NOT NULL" in sql
    assert "NTEXT" not in sql  # the deprecated type
    assert "CONSTRAINT ck_messages_role_valid CHECK (role IN ('user', 'assistant'))" in sql
    assert (
        "CONSTRAINT ck_messages_citations_json CHECK (citations IS NULL OR ISJSON(citations) = 1)"
        in sql
    )
    assert "CONSTRAINT uq_messages_seq UNIQUE (seq)" in sql
    assert "ON DELETE NO ACTION" in sql
    assert "CREATE INDEX ix_conversations_updated_at ON conversations (updated_at DESC)" in sql
    assert "CREATE INDEX ix_messages_conversation_id_seq ON messages (conversation_id, seq)" in sql


def test_the_migration_and_the_models_produce_the_same_ddl() -> None:
    """Offline drift check: every model column/constraint line is in the revision and vice versa."""
    sql = _upgrade_sql()
    dialect = mssql.dialect()
    # A model added without a migration (or the reverse) must fail here, not pass silently.
    assert set(Base.metadata.tables) == {"conversations", "messages"}
    for table in Base.metadata.sorted_tables:
        wrapper = {f"CREATE TABLE {table.name} (", ")"}
        expected = [
            x for x in _lines(str(CreateTable(table).compile(dialect=dialect))) if x not in wrapper
        ]
        actual = [x for x in _lines(_table_block(sql, table.name)) if x not in wrapper]
        assert actual == expected, table.name
        for index in table.indexes:
            statement = str(CreateIndex(index).compile(dialect=dialect)).strip()
            assert statement in sql, index.name


def test_downgrade_drops_children_first_and_removes_the_version_row() -> None:
    sql = _downgrade_sql()
    order = [
        "DROP INDEX ix_messages_conversation_id_seq ON messages",
        "DROP TABLE messages",
        "DROP INDEX ix_conversations_updated_at ON conversations",
        "DROP TABLE conversations",
        f"DELETE FROM alembic_version WHERE alembic_version.version_num = '{REVISION}'",
    ]
    positions = [sql.index(statement) for statement in order]
    assert positions == sorted(positions)


def test_downgrade_mirrors_upgrade() -> None:
    """Every object the upgrade creates, the downgrade drops."""
    up, down = _upgrade_sql(), _downgrade_sql()
    for table in ("conversations", "messages"):
        assert f"CREATE TABLE {table}" in up and f"DROP TABLE {table}" in down
    for index in ("ix_conversations_updated_at", "ix_messages_conversation_id_seq"):
        assert f"CREATE INDEX {index}" in up and f"DROP INDEX {index}" in down
