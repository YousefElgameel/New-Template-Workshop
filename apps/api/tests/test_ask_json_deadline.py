# ruff: noqa: F811 - pytest fixtures imported from a sibling test module and used as parameters
"""The JSON ``/ask`` route has the same overall deadline as the stream (api 5.6): a graph that never
answers ends in ``503 ai_unavailable`` after the configured worst case, not after the ingress has
cut the connection."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import app.ai.limits as limits_mod
from tests.test_ask_api import (  # noqa: F401 - the fixtures are used by name
    AskSession,
    _isolation,
    ask,
    assert_error,
    conversation,
    make_client,
)


class NeverAnswers:
    """A graph whose model call never returns."""

    async def ainvoke(self, inputs: Any, config: Any = None) -> Any:
        await asyncio.sleep(30)
        raise AssertionError("never reached")  # pragma: no cover


@pytest.fixture(autouse=True)
def _tiny_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # budget = 0.05 * (0 + 1) + 2 * 0.05 = 0.15 seconds
    monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("AI_MAX_RETRIES", "0")
    monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", "0.05")


def test_a_model_that_never_answers_ends_in_503_after_the_configured_worst_case(
    make_client: Any,
) -> None:
    conv = conversation()
    session = AskSession(conv)
    started = time.perf_counter()
    response = ask(make_client(session, NeverAnswers()), conv.id)
    elapsed = time.perf_counter() - started
    assert_error(response, 503, "ai_unavailable")
    assert 0.1 < elapsed < 5  # the deadline, not the 30 s the graph would have taken
    assert [m.role for m in session.added] == ["user"]  # the question is kept, no answer stored
    assert limits_mod.slots_in_use() == 0


def test_the_deadline_is_the_worst_case_from_the_settings(
    make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Longer budget: 0.5 * 1 + 2 * 0.5 = 1.5 s — the route waits for it.
    monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", "0.5")
    conv = conversation()
    started = time.perf_counter()
    response = ask(make_client(AskSession(conv), NeverAnswers()), conv.id)
    elapsed = time.perf_counter() - started
    assert_error(response, 503, "ai_unavailable")
    assert 1.4 < elapsed < 6


def test_a_prompt_answer_is_not_affected(make_client: Any) -> None:
    from tests.test_ask_api import CHUNKS, graph_for, recording_chat_model

    conv = conversation()
    graph = graph_for(recording_chat_model("The api listens on port 8000 [1]."), CHUNKS)
    response = ask(make_client(AskSession(conv), graph), conv.id)
    assert response.status_code == 200 and response.json()["answer"].startswith("The api listens")
