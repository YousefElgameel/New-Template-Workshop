"""The dev smoke runbook (api roadmap 5.1) is checked against the code it describes, offline.

It is read by a person at a terminal, so what it says must stay true: the order of the steps, the
routes, the error codes and kinds, the tables, and the setting names. Nothing here runs a command
from it, and nothing in it may look like a secret.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.ai.ingest_job as ingest_job
from app.db import Base

REPO = Path(__file__).resolve().parents[3]
RUNBOOKS = REPO / "docs" / "operations" / "runbooks"
SMOKE = RUNBOOKS / "team-assistant-dev-smoke.md"


@pytest.fixture(scope="module")
def text() -> str:
    return SMOKE.read_text(encoding="utf-8")


def test_the_steps_come_in_the_order_the_chain_depends_on(text: str) -> None:
    headings = re.findall(r"^## (\d)\. ", text, re.M)
    assert headings == ["0", "1", "2", "3", "4", "5", "6", "7", "8"][: len(headings)]
    assert len(headings) == 9
    order = [text.index(f"## {n}. ") for n in range(9)]
    assert order == sorted(order)


def test_every_table_the_models_create_is_checked_in_step_one(text: str) -> None:
    step = text.split("## 1. ")[1].split("## 2. ")[0]
    for table in Base.metadata.tables:
        assert f"'{table}'" in step, table


def test_the_routes_it_calls_exist() -> None:
    from app.main import app

    served = {(route.path, m) for route in app.routes for m in getattr(route, "methods", ())}
    for path, method in (
        ("/v1/admin/ingest", "POST"),
        ("/v1/conversations", "POST"),
        ("/v1/conversations/{conversation_id}", "GET"),
        ("/v1/conversations/{conversation_id}/ask", "POST"),
        ("/v1/conversations/{conversation_id}/ask/stream", "POST"),
        ("/health", "GET"),
    ):
        assert (path, method) in served, (path, method)


def test_the_routes_it_names_are_the_ones_in_the_text(text: str) -> None:
    for needle in (
        "$API/v1/admin/ingest",
        "$API/v1/conversations",
        "$WEB/api/v1/conversations",
        "/ask/stream",
        "x-admin-key",
    ):
        assert needle in text, needle


def test_every_ingest_error_kind_it_names_is_a_real_one(text: str) -> None:
    real = {kind for _, kind in ingest_job._KINDS} | {"ingest_error", "unexpected"}
    step = text.split("## 4. ")[1].split("## 5. ")[0]
    named = set(re.findall(r"`([a-z_]+)`", step.partition("`error_kind`")[2]))
    kinds = {k for k in named if k in real or k.startswith(("corpus_", "dimension", "index_"))}
    assert kinds, "no error kinds found"
    assert kinds <= real, kinds - real
    for kind in ("dimension_mismatch", "index_shape", "timeout", "lock_lost", "not_configured"):
        assert kind in real and kind in step


def test_the_failure_signatures_the_roadmap_asks_for_are_there(text: str) -> None:
    assert "dimension_mismatch" in text and "AZURE_AI_EMBEDDING_DIMENSIONS" in text
    assert "401" in text and "unauthorized" in text and "x-admin-key" in text
    assert "ODBC Driver 18" in text and "IM002" in text


def test_the_two_checks_added_from_the_streaming_review_are_there(text: str) -> None:
    assert "one by one" in text and "X-Accel-Buffering".lower() in text.lower()
    assert "7b." in text and "mid-answer" in text and "commit" in text


def test_the_stream_frames_it_expects_are_the_ones_the_api_sends(text: str) -> None:
    streaming = (REPO / "apps" / "api" / "app" / "routers" / "ask.py").read_text(encoding="utf-8")
    for frame in ("sources", "token", "done", "error"):
        assert f"`{frame}`" in streaming, frame
        assert f"event: {frame}" in text or f"`event: {frame}`" in text or frame == "error"


def test_it_never_carries_a_secret_or_a_real_host(text: str) -> None:
    assert not re.search(r"AKIA|sk-[A-Za-z0-9]{10}|ghp_|eyJ[A-Za-z0-9]{10}", text)
    assert not re.search(r"(?i)(password|accountkey|sharedaccesskey)\s*=", text)
    assert not re.search(r"[a-z0-9-]+\.(database\.windows\.net|search\.windows\.net)", text)
    assert "read -rs ADMIN_KEY" in text  # the key is read silently, never written in the file


def test_every_relative_link_resolves(text: str) -> None:
    for target in re.findall(r"\]\((?!https?:|#)([^)#\s]+)", text):
        assert (SMOKE.parent / target).resolve().exists(), target


def test_the_runbook_is_in_the_index() -> None:
    readme = (RUNBOOKS / "README.md").read_text(encoding="utf-8")
    assert "(team-assistant-dev-smoke.md)" in readme
