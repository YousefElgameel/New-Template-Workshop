"""The startup check that one answer's worst case fits the ingress request timeout (api 5.6)."""

from __future__ import annotations

import pytest
import structlog.testing
from fastapi.testclient import TestClient

import app.main as main_mod

EVENT = "the ask deadline is longer than the ingress request timeout"


@pytest.fixture(autouse=True)
def _clean_ai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AI_REQUEST_TIMEOUT_SECONDS",
        "AI_MAX_RETRIES",
        "AI_RETRIEVAL_TIMEOUT_SECONDS",
        "AZURE_AI_AUTH_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


def _events() -> list[dict]:
    with structlog.testing.capture_logs() as logs:
        main_mod._warn_if_the_ask_deadline_is_too_long()
    return [e for e in logs if e["event"] == EVENT]


def test_the_defaults_are_quiet() -> None:
    assert _events() == []


def test_a_deadline_over_the_margin_logs_one_warning_with_numbers_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_MAX_RETRIES", "2")  # 60 s x 3 + 2 x 30 s = 240 s
    (event,) = _events()
    assert event["log_level"] == "warning"
    assert event["worst_case_seconds"] == 240.0
    assert event["limit_seconds"] == 220.0 and event["ingress_seconds"] == 240.0
    assert set(event) == {
        "event",
        "log_level",
        "worst_case_seconds",
        "limit_seconds",
        "ingress_seconds",
    }


def test_a_larger_timeout_alone_can_cross_the_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "120")  # 120 s x 2 + 60 s = 300 s
    (event,) = _events()
    assert event["worst_case_seconds"] == 300.0


def test_a_bad_ai_setting_does_not_stop_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_AI_AUTH_MODE", "nonsense")
    assert _events() == []  # reported where the setting is used, not here


def test_the_app_starts_and_warns_through_the_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MAX_RETRIES", "3")
    with structlog.testing.capture_logs() as logs:
        with TestClient(main_mod.create_app()):
            pass
    warnings = [e for e in logs if e["event"] == EVENT]
    assert len(warnings) == 1 and warnings[0]["worst_case_seconds"] == 300.0
