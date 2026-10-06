"""The conversation/message models match ``docs/design/db-design.md``.

Everything here is offline: the DDL is compiled for the SQL Server dialect, never executed.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import mssql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  — registers the models on Base.metadata
from app.db import Base
from app.models.conversation import (
    DEFAULT_CONVERSATION_TITLE,
    MESSAGE_ROLES,
    Conversation,
    Message,
)


def _ddl(table) -> str:
    return str(CreateTable(table).compile(dialect=mssql.dialect()))


def test_models_are_registered_for_alembic_autogenerate() -> None:
    assert {"conversations", "messages"} <= set(Base.metadata.tables)


def test_conversations_table_ddl() -> None:
    ddl = _ddl(Conversation.__table__)
    assert "id UNIQUEIDENTIFIER NOT NULL" in ddl
    assert "title NVARCHAR(200) NOT NULL" in ddl
    assert ddl.count("DATETIME2(3) NOT NULL DEFAULT sysutcdatetime()") == 2
    assert "CONSTRAINT pk_conversations PRIMARY KEY (id)" in ddl


def test_messages_table_ddl() -> None:
    ddl = _ddl(Message.__table__)
    assert "seq BIGINT NOT NULL IDENTITY(1,1)" in ddl
    assert "CONSTRAINT uq_messages_seq UNIQUE (seq)" in ddl
    assert "role NVARCHAR(20) NOT NULL" in ddl
    assert "content NVARCHAR(max) NOT NULL" in ddl
    assert "NTEXT" not in ddl  # the deprecated type; offline DDL must say NVARCHAR(max)
    assert "citations NVARCHAR(max) NULL" in ddl
    assert "token_count INTEGER" in ddl
    assert "CONSTRAINT ck_messages_role_valid CHECK (role IN ('user', 'assistant'))" in ddl
    assert (
        "CONSTRAINT ck_messages_citations_json CHECK (citations IS NULL OR ISJSON(citations) = 1)"
        in ddl
    )
    assert (
        "CONSTRAINT fk_messages_conversation_id_conversations FOREIGN KEY(conversation_id) "
        "REFERENCES conversations (id) ON DELETE NO ACTION"
    ) in ddl


def test_citations_and_token_count_are_nullable_but_the_rest_is_not() -> None:
    columns = Message.__table__.c
    assert columns.citations.nullable and columns.token_count.nullable
    for name in ("id", "seq", "conversation_id", "role", "content", "created_at"):
        assert not columns[name].nullable, name


def test_indexes_support_the_list_and_the_thread_reads() -> None:
    conv_index = next(i for i in Conversation.__table__.indexes)
    assert conv_index.name == "ix_conversations_updated_at"
    assert "updated_at DESC" in str(CreateIndex(conv_index).compile(dialect=mssql.dialect()))

    msg_index = next(i for i in Message.__table__.indexes)
    assert msg_index.name == "ix_messages_conversation_id_seq"
    assert [c.name for c in msg_index.columns] == ["conversation_id", "seq"]


def test_python_side_defaults_fill_ids_title_and_timestamps() -> None:
    column = Conversation.__table__.c
    assert column.title.default.arg == DEFAULT_CONVERSATION_TITLE
    assert column.id.default is not None
    assert column.created_at.default is not None and column.updated_at.default is not None


def test_allowed_roles() -> None:
    assert MESSAGE_ROLES == ("user", "assistant")


@pytest.mark.parametrize("table", [Conversation.__table__, Message.__table__])
def test_every_string_column_has_an_explicit_length_or_is_max(table) -> None:
    ddl = _ddl(table)
    assert "VARCHAR(max)" not in ddl.replace("NVARCHAR(max)", "")


def test_role_check_is_built_from_the_allowed_roles() -> None:
    assert "role IN ('user', 'assistant')" in _ddl(Message.__table__)


def test_utcnow_is_naive_utc_truncated_to_milliseconds() -> None:
    from app.models.conversation import utcnow

    now = utcnow()
    assert now.tzinfo is None
    assert now.microsecond % 1000 == 0


def test_citations_serialise_as_json_text_and_none_is_sql_null() -> None:
    column_type = Message.__table__.c.citations.type
    serialize = column_type.bind_processor(mssql.dialect())
    assert serialize([]) == "[]"
    arabic = serialize([{"title": "دليل", "path": "docs/a.md"}])
    assert '"path": "docs/a.md"' in arabic and r"\u062f" in arabic  # valid JSON, ASCII-escaped
    assert column_type.none_as_null is True
