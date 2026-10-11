"""The conversation purge runbook (api roadmap 5.8): its statements are reviewed like code, offline.

No agent runs them. These tests keep the runbook from drifting from the models and pin the
properties that make it safe for a person to run: the table and column names are the real ones,
messages are deleted before their conversation (the foreign key is ``NO ACTION``), every delete
batch refuses to run until the expected counts are set, holds what it removes, stops for a recent
cutoff, commits only when told to and when the counts match, and rolls back otherwise; the dry run
and the delete use the same predicate; nothing is built by pasting text into SQL.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.models.conversation import TITLE_MAX_LENGTH, Conversation, Message

REPO = Path(__file__).resolve().parents[3]
RUNBOOKS = REPO / "docs" / "operations" / "runbooks"
PURGE = RUNBOOKS / "purge-conversations.md"

CONVERSATION_COLUMNS = {c.name for c in Conversation.__table__.columns}
MESSAGE_COLUMNS = {c.name for c in Message.__table__.columns}
ZERO_ID = "00000000-0000-0000-0000-000000000000"


def _text() -> str:
    return PURGE.read_text(encoding="utf-8")


def _blocks(label: str) -> list[str]:
    return re.findall(rf"^[ \t]*```{label}\n(.*?)^[ \t]*```", _text(), re.S | re.M)


def _sql_blocks() -> list[str]:
    return _blocks("sql")


def _code(block: str) -> str:
    """The block without comments, so a comment can neither satisfy nor break a check."""
    return re.sub(r"--[^\n]*", "", block)


def _is_delete(block: str) -> bool:
    return bool(re.search(r"\bDELETE\b", _code(block)))


def _delete_blocks() -> list[str]:
    return [b for b in _sql_blocks() if _is_delete(b)]


def _procedure(name: str) -> str:
    return next(b for b in _delete_blocks() if name in b)


def _dry_run(predicate: str) -> str:
    return next(b for b in _sql_blocks() if not _is_delete(b) and predicate in b)


class TestStatementsMatchTheModels:
    def test_the_tables_are_the_models_tables(self) -> None:
        tables = set(re.findall(r"\bdbo\.(\w+)", "\n".join(_sql_blocks())))
        assert tables == {Conversation.__tablename__, Message.__tablename__}

    def test_the_prose_column_lists_are_the_real_columns(self) -> None:
        text = _text()
        for table, columns in (
            ("conversations", CONVERSATION_COLUMNS),
            ("messages", MESSAGE_COLUMNS),
        ):
            listed = re.search(rf"`dbo\.{table}` \(([^)]*)\)", text)
            assert listed, table
            assert set(re.findall(r"`(\w+)`", listed.group(1))) == columns, table

    def test_every_qualified_column_exists_on_its_table(self) -> None:
        sql = _code("\n".join(_sql_blocks()))
        for alias, columns in (("c", CONVERSATION_COLUMNS), ("m", MESSAGE_COLUMNS)):
            for column in re.findall(rf"\b{alias}\.(\w+)", sql):
                assert column in columns, (alias, column)

    def test_every_bare_column_compared_in_a_where_is_a_real_column(self) -> None:
        known = CONVERSATION_COLUMNS | MESSAGE_COLUMNS
        sql = _code("\n".join(_sql_blocks()))
        compared = re.findall(r"\b(?:WHERE|AND|OR)\s+(\w+)\s*(?:=|<|>|LIKE\b)", sql)
        assert compared, "no comparisons found: the pattern no longer reads the runbook"
        for word in compared:
            assert word in known, word

    def test_the_title_length_in_prose_is_the_columns(self) -> None:
        assert f"{TITLE_MAX_LENGTH} characters" in _text()

    def test_the_foreign_key_the_order_relies_on_is_still_no_action(self) -> None:
        (fk,) = Message.__table__.foreign_keys
        assert fk.ondelete == "NO ACTION"

    def test_the_api_still_has_no_delete_endpoint_so_this_is_still_the_only_way(self) -> None:
        document = REPO / "docs" / "reference" / "openapi.json"
        paths = json.loads(document.read_text(encoding="utf-8"))["paths"]
        for path, operations in paths.items():
            assert "delete" not in operations, path
        assert "get" in paths["/v1/conversations/{conversation_id}"]  # the verify step uses it


class TestEveryDeleteBatch:
    def test_there_are_exactly_two_one_per_procedure(self) -> None:
        deletes = _delete_blocks()
        assert len(deletes) == 2
        assert any("@cutoff" in b for b in deletes)
        assert any("conversation_id = @id" in b for b in deletes)

    def test_messages_are_deleted_before_their_conversation(self) -> None:
        for block in _delete_blocks():
            code = _code(block)
            messages = re.search(r"DELETE\s+(?:m\s+FROM|FROM)\s+dbo\.messages", code)
            conversations = re.search(r"DELETE\s+FROM\s+dbo\.conversations", code)
            assert messages and conversations, block
            assert messages.start() < conversations.start(), "the foreign key is NO ACTION"

    def test_xact_abort_precedes_the_transaction_which_precedes_every_delete(self) -> None:
        for block in _delete_blocks():
            code = _code(block)
            assert code.index("SET XACT_ABORT ON") < code.index("BEGIN TRANSACTION")
            assert code.index("BEGIN TRANSACTION") < code.index("DELETE")

    def test_the_commit_is_conditional_and_everything_else_rolls_back(self) -> None:
        for block in _delete_blocks():
            code = _code(block)
            tail = " ".join(code.split()[-12:])
            assert re.search(r"COMMIT TRANSACTION; ELSE ROLLBACK TRANSACTION;$", tail), tail
            assert code.count("COMMIT TRANSACTION") == 1  # only inside the IF
            commit_at = code.index("COMMIT TRANSACTION")
            condition = code[code.rindex("IF ", 0, commit_at) : commit_at]
            assert "@commit = 1" in condition
            assert "@messages_deleted = @expected_messages" in condition

    def test_the_commit_switch_starts_off(self) -> None:
        for block in _delete_blocks():
            assert "DECLARE @commit bit = 0;" in block

    def test_the_batch_refuses_to_run_until_the_expected_counts_are_set(self) -> None:
        for block in _delete_blocks():
            assert "DECLARE @expected_messages int = -1;" in block
            assert re.search(r"IF @expected_\w+ < 0.*THROW 5000\d", _code(block))
        b2 = _procedure("@cutoff")
        assert "DECLARE @expected_conversations int = -1;" in b2
        assert "IF @expected_conversations < 0 OR @expected_messages < 0 THROW" in b2

    def test_row_counts_are_taken_immediately_after_each_delete(self) -> None:
        for block in _delete_blocks():
            code = _code(block)
            for table, name in (
                ("messages", "messages_deleted"),
                ("conversations", "conversations_deleted"),
            ):
                pattern = (
                    rf"DELETE\s+(?:m\s+)?FROM\s+dbo\.{table}\b[^;]*;"
                    rf"\s*DECLARE @{name} int = @@ROWCOUNT;"
                )
                assert re.search(pattern, code), (table, block)
            assert code.count("@@ROWCOUNT") == 2

    def test_what_is_to_be_deleted_is_held_so_nothing_can_be_added_to_it(self) -> None:
        a3, b2 = _procedure("conversation_id = @id"), _procedure("@cutoff")
        assert "dbo.conversations WITH (UPDLOCK, HOLDLOCK) WHERE id = @id" in a3
        assert (
            "INTO #doomed FROM dbo.conversations WITH (UPDLOCK, HOLDLOCK) "
            "WHERE updated_at < @cutoff" in b2
        )

    def test_the_cutoff_procedure_deletes_by_a_snapshot_never_by_the_date_twice(self) -> None:
        code = _code(_procedure("@cutoff"))
        assert "m.conversation_id IN (SELECT id FROM #doomed)" in code
        assert "WHERE id IN (SELECT id FROM #doomed)" in code
        assert code.count("updated_at < @cutoff") == 1  # only where the snapshot is taken
        assert "m.created_at" not in code  # never old messages out of a live conversation

    def test_the_delete_reports_its_counts_for_comparison_with_the_dry_run(self) -> None:
        report = (
            "SELECT @conversations_deleted AS conversations_deleted, "
            "@messages_deleted AS messages_deleted;"
        )
        for block in _delete_blocks():
            assert report in block

    def test_every_delete_statement_has_a_real_where_clause(self) -> None:
        for block in _delete_blocks():
            for statement in _code(block).split(";"):
                if re.search(r"\bDELETE\b", statement):
                    where = re.search(r"\bWHERE\b(.*)", statement, re.S)
                    assert where, statement
                    assert re.search(r"@id|#doomed", where.group(1)), statement
                    assert not re.search(r"\b1\s*=\s*1\b", statement)


class TestPlaceholdersAreHarmless:
    def test_the_only_id_in_the_runbook_is_the_zero_id(self) -> None:
        sql = "\n".join(_sql_blocks())
        for found in re.findall(r"'([0-9a-f]{8}-[0-9a-f-]{27})'", sql, re.I):
            assert found == ZERO_ID

    def test_the_sample_cutoff_is_a_date_that_matches_nothing_real(self) -> None:
        for block in (_procedure("@cutoff"), _dry_run("conversations_total")):
            assert "DECLARE @cutoff datetime2(3) = '1900-01-01T00:00:00';" in block

    def test_a_recent_cutoff_stops_both_the_dry_run_and_the_delete(self) -> None:
        for block in (_procedure("@cutoff"), _dry_run("conversations_total")):
            assert "IF @cutoff > DATEADD(DAY, -30, SYSUTCDATETIME()) THROW 50003" in block

    def test_the_dry_run_and_the_delete_use_the_same_predicate(self) -> None:
        # B: the dry run counts conversations and messages with the delete's `updated_at < @cutoff`
        b1 = _dry_run("conversations_total")
        assert b1.count("updated_at < @cutoff") == 4  # two CASEs, the messages count, the id list
        assert "SELECT id FROM dbo.conversations WHERE updated_at < @cutoff" in b1
        # A: the dry run counts with the delete's `id = @id` / `conversation_id = @id`
        a2 = _dry_run("COUNT(*) FROM dbo.conversations WHERE id = @id")
        assert "COUNT(*) FROM dbo.messages WHERE conversation_id = @id" in a2
        a3 = _procedure("conversation_id = @id")
        assert "DELETE FROM dbo.messages WHERE conversation_id = @id;" in a3
        assert "DELETE FROM dbo.conversations WHERE id = @id;" in a3

    def test_the_dry_runs_change_nothing(self) -> None:
        for block in _sql_blocks():
            if not _is_delete(block):
                code = _code(block)
                assert not re.search(r"\b(?:DELETE|UPDATE|INSERT|MERGE|INTO #)", code), block


class TestNoDynamicSqlAndNothingDestructiveBeyondTheDeletes:
    def test_every_statement_uses_variables_and_builds_no_sql_text(self) -> None:
        for block in _sql_blocks():
            code = _code(block)
            if "FROM sys.tables" in code:
                continue  # the schema check has no input at all
            assert re.search(r"DECLARE\s+@\w+", code), block
            assert not re.search(r"\bEXEC(UTE)?\b|sp_executesql|\bxp_\w+", code, re.I)
            for match in re.finditer(
                r"\+\s*@(\w+)", code
            ):  # the only concatenation: a LIKE pattern
                assert match.group(1) in {"term", "first_words"}, match.group(0)

    def test_the_search_shows_ids_and_dates_not_text(self) -> None:
        search = _dry_run("@term")
        select = re.split(r"\bFROM\b", search, flags=re.I)[0]
        assert "content" not in select.lower() and "title" not in select.lower()

    def test_the_confirmation_is_a_yes_or_no_not_the_title(self) -> None:
        confirm = _dry_run("@first_words")
        assert "'yes'" in confirm and "'no'" in confirm
        assert not re.search(r"SELECT\s+(?:c\.)?title\b", confirm, re.I)

    @pytest.mark.parametrize(
        "word",
        [
            "DROP",
            "TRUNCATE",
            "ALTER",
            "GRANT",
            "CREATE",
            "UPDATE",
            "INSERT",
            "MERGE",
            "USE",
            "SHUTDOWN",
        ],
    )
    def test_nothing_destructive_in_the_run_blocks(self, word: str) -> None:
        for block in _sql_blocks():
            assert not re.search(rf"\b{word}\b", _code(block), re.I), word

    def test_the_admin_statements_give_select_and_delete_on_two_tables_and_take_them_back(
        self,
    ) -> None:
        (admin,) = _blocks("sql-admin")
        code = _code(admin)
        grants = re.findall(r"GRANT ([A-Z, ]+) ON OBJECT::dbo\.(\w+) TO conversation_purger;", code)
        assert sorted(grants) == [
            ("SELECT, DELETE", "conversations"),
            ("SELECT, DELETE", "messages"),
        ]
        assert "ADD MEMBER" in code and "DROP MEMBER" in code and "DROP USER" in code
        broad = r"\b(?:db_owner|db_datawriter|db_datareader|WITH GRANT OPTION|CONTROL|ALTER ANY)\b"
        assert not re.search(broad, code)
        assert not re.search(r"GRANT (?:ALL|INSERT|UPDATE)", code)


class TestRunbook:
    def _section(self, heading: str) -> str:
        match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", _text(), re.S | re.M)
        assert match, heading
        return match.group(1)

    def test_it_has_the_sections_the_acceptance_requires(self) -> None:
        for heading in (
            "What this removes, and what it does not",
            "Before you start",
            "Procedure A — one conversation",
            "Procedure B — every conversation not touched since a date",
            "Grant and revoke (the database owner runs these, not you)",
            "Verify",
            "Record it (no content)",
            "Retention decision",
            "If it goes wrong",
            "References",
        ):
            self._section(heading)

    def test_the_retention_decision_is_stated(self) -> None:
        retention = self._section("Retention decision").lower()
        assert "no automatic retention for now" in retention
        assert "revisit" in retention and "authentication lands" in retention
        assert "adr-0014" in retention

    def test_it_says_no_agent_runs_it_and_that_the_data_is_shared_and_personal(self) -> None:
        text = _text()
        assert "No agent runs any statement below" in text
        assert "**shared**" in text and "personal data" in text and "ADR-0014" in text

    def test_it_says_what_pii_purge_covers(self) -> None:
        text = _text()
        assert "telemetry only" in text and "(pii-purge.md)" in text

    def test_it_covers_backups_restores_and_the_model_service_honestly(self) -> None:
        text = _text()
        assert "7 days" in text and "1 to 35" in text and "LTR" in text
        # a restore brings removed rows back: both the table and the failure section say to replay
        assert "replay every earlier removal from the request log" in " ".join(text.split())
        assert "must then be **replayed** from the request log" in " ".join(text.split())
        assert "not verified here" in text  # the model service's retention is not claimed as fact

    def test_it_warns_that_the_search_phrase_is_kept_in_statement_text(self) -> None:
        text = _text()
        assert "Query Store" in text and "batch text" in text

    def test_the_decisions_are_cited_to_architecture_b8_not_db_design(self) -> None:
        text = _text()
        assert "[`ARCHITECTURE.md`](../../architecture/ARCHITECTURE.md) B8" in text
        assert not re.search(r"decisions 5 and 6 of\s+\[`db-design.md`\]", text)

    def test_it_holds_no_secret_or_connection_string(self) -> None:
        text = _text()
        for forbidden in ("Password=", "pwd=", "Server=tcp", "AccountKey", "sig=", "Bearer "):
            assert forbidden not in text

    def test_it_is_listed_in_the_readme_and_linked_from_pii_purge(self) -> None:
        assert "(purge-conversations.md)" in (RUNBOOKS / "README.md").read_text(encoding="utf-8")
        pii = (RUNBOOKS / "pii-purge.md").read_text(encoding="utf-8")
        assert "purge-conversations.md" in pii and "Telemetry only" in pii

    def test_every_runbook_has_a_row_in_the_readme(self) -> None:
        readme = (RUNBOOKS / "README.md").read_text(encoding="utf-8")
        for path in sorted(RUNBOOKS.glob("*.md")):
            if path.name != "README.md":
                assert f"({path.name})" in readme, path.name

    def test_every_relative_link_points_at_a_file(self) -> None:
        for target in re.findall(r"\]\((?!https?:|#)([^)#]+)", _text()):
            assert (PURGE.parent / target).resolve().exists(), target

    def test_the_threat_model_points_here_and_g11_is_not_ticked_before_it_is_used(self) -> None:
        path = REPO / "docs" / "security" / "threat-models" / "team-assistant.md"
        model = path.read_text(encoding="utf-8")
        assert "purge-conversations.md" in model
        assert re.search(r"^- \[ \] G11:", model, re.M)
