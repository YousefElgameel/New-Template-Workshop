"""``describe_database_target`` names the database a tool is about to touch — never the user or
the password (it feeds the one line ``alembic/env.py`` logs before it connects)."""

from __future__ import annotations

import pytest

from app.config import DEFAULT_DATABASE_URL, PLACEHOLDER_DATABASE_HOST, describe_database_target

SECRET = "S3CR3T-PW-DO-NOT-PRINT"


def test_it_names_host_port_and_database_without_credentials() -> None:
    url = (
        f"mssql+aioodbc://dev-user:{SECRET}@sql-dev.example.net:1433/team-assistant"
        "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
    )
    described = describe_database_target(url)
    assert described == "sql-dev.example.net:1433/team-assistant"
    assert SECRET not in described and "dev-user" not in described


def test_it_flags_the_template_placeholder() -> None:
    described = describe_database_target(DEFAULT_DATABASE_URL)
    assert PLACEHOLDER_DATABASE_HOST in described
    assert "not configured" in described
    assert "PASSWORD" not in described and "USER" not in described


def test_a_managed_identity_url_has_no_password_to_show() -> None:
    url = (
        "mssql+aioodbc://client-id@sql-prod.example.net:1433/app"
        "?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi"
    )
    assert describe_database_target(url) == "sql-prod.example.net:1433/app"


@pytest.mark.parametrize("bad", ["", "not a url", f"mssql+aioodbc://u:{SECRET}@/%%%", "://:@"])
def test_an_unparsable_url_never_echoes_the_input(bad: str) -> None:
    assert SECRET not in describe_database_target(bad)
