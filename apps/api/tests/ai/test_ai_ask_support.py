"""The pieces behind ``POST /v1/conversations/{id}/ask`` (api 4.2): history mapping, the concurrency
slots, the three new settings, the output bound on the model client, and retrieval timeouts."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import app.ai.client as client_mod
import app.ai.limits as limits
from app.ai.config import (
    DEFAULT_MAX_CONCURRENT_ASKS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_RETRIEVAL_TIMEOUT_SECONDS,
    get_ai_settings,
)
from app.ai.history import HISTORY_MESSAGES, MAX_HISTORY_CHARS, history_messages
from app.ai.tools.retrieve import AzureSearchRetriever
from tests.ai.fakes import FakeEmbeddings, FakeSearchClient

NEW_ENV = ("AI_MAX_OUTPUT_TOKENS", "AI_RETRIEVAL_TIMEOUT_SECONDS", "AI_MAX_CONCURRENT_ASKS")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for name in (*NEW_ENV, "AZURE_AI_ENDPOINT", "AZURE_AI_DEPLOYMENT", "AZURE_AI_AUTH_MODE"):
        monkeypatch.delenv(name, raising=False)
    limits._reset_for_tests()
    client_mod._reset_for_tests()
    yield
    limits._reset_for_tests()
    client_mod._reset_for_tests()


# ── history ──────────────────────────────────────────────────────────────────


def row(role: str, content: object) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, id="private-id", citations=[{"x": 1}])


class TestHistoryMessages:
    def test_user_and_assistant_rows_become_human_and_ai_messages_in_order(self) -> None:
        out = history_messages([row("user", "q1"), row("assistant", "a1"), row("user", "q2")])
        assert [type(m) for m in out] == [HumanMessage, AIMessage, HumanMessage]
        assert [m.content for m in out] == ["q1", "a1", "q2"]

    @pytest.mark.parametrize("role", ["system", "tool", "developer", "", "User", "ASSISTANT", None])
    def test_any_other_role_is_dropped_and_never_a_system_message(self, role: object) -> None:
        out = history_messages([row("user", "kept"), row(role, "SECRET-STORED-TEXT")])  # type: ignore[arg-type]
        assert [m.content for m in out] == ["kept"]
        assert not any(isinstance(m, SystemMessage) for m in out)

    def test_a_row_without_text_is_skipped(self) -> None:
        assert history_messages([row("user", None), row("assistant", 5)]) == []

    def test_only_the_text_is_passed_on(self) -> None:
        (message,) = history_messages([row("user", "hello")])
        assert "private-id" not in repr(message) and "citations" not in repr(message.model_dump())

    def test_a_replayed_row_is_cut_to_the_question_length(self) -> None:
        out = history_messages([row("user", "q" * 9_000), row("assistant", "a" * 9_000)])
        assert [len(m.content) for m in out] == [MAX_HISTORY_CHARS, MAX_HISTORY_CHARS]
        assert MAX_HISTORY_CHARS == 4_000
        short = history_messages([row("user", "short")])
        assert short[0].content == "short"

    def test_empty_and_the_limit_constant(self) -> None:
        assert history_messages([]) == []
        assert HISTORY_MESSAGES == 10


# ── the concurrency slots ────────────────────────────────────────────────────


class TestAskSlots:
    def test_slots_are_counted_and_released(self) -> None:
        assert limits.slots_in_use() == 0
        with limits.ask_slot(2):
            assert limits.slots_in_use() == 1
            with limits.ask_slot(2):
                assert limits.slots_in_use() == 2
        assert limits.slots_in_use() == 0

    def test_one_more_than_the_limit_is_refused_and_does_not_leak(self) -> None:
        with limits.ask_slot(1):
            with pytest.raises(limits.NoFreeSlot):
                with limits.ask_slot(1):
                    pytest.fail("must not enter")
            assert limits.slots_in_use() == 1
        assert limits.slots_in_use() == 0

    def test_an_exception_in_the_block_releases_the_slot(self) -> None:
        with pytest.raises(RuntimeError):
            with limits.ask_slot(1):
                raise RuntimeError("boom")
        assert limits.slots_in_use() == 0

    def test_a_cancelled_task_releases_its_slot(self) -> None:
        async def scenario() -> None:
            async def holder() -> None:
                with limits.ask_slot(1):
                    await asyncio.sleep(30)

            task = asyncio.create_task(holder())
            await asyncio.sleep(0.01)
            assert limits.slots_in_use() == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert limits.slots_in_use() == 0

    def test_a_limit_of_zero_or_less_refuses_everything(self) -> None:
        with pytest.raises(limits.NoFreeSlot):
            with limits.ask_slot(0):
                pass


# ── settings ─────────────────────────────────────────────────────────────────


class TestNewSettings:
    def test_defaults(self) -> None:
        s = get_ai_settings()
        assert (s.max_output_tokens, s.retrieval_timeout_seconds, s.max_concurrent_asks) == (
            1024,
            30.0,
            4,
        )
        assert (DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_MAX_CONCURRENT_ASKS) == (1024, 4)
        assert DEFAULT_RETRIEVAL_TIMEOUT_SECONDS == 30.0

    def test_values_are_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", "256")
        monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", "7.5")
        monkeypatch.setenv("AI_MAX_CONCURRENT_ASKS", "9")
        s = get_ai_settings()
        assert (s.max_output_tokens, s.retrieval_timeout_seconds, s.max_concurrent_asks) == (
            256,
            7.5,
            9,
        )

    @pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-3", "1.5", "nan"])
    def test_an_unusable_integer_setting_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", bad)
        monkeypatch.setenv("AI_MAX_CONCURRENT_ASKS", bad)
        s = get_ai_settings()
        assert s.max_output_tokens == 1024 and s.max_concurrent_asks == 4

    @pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-3", "nan"])
    def test_an_unusable_timeout_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", bad)
        assert get_ai_settings().retrieval_timeout_seconds == 30.0


# ── the output bound on the real model client ────────────────────────────────


def test_the_chat_client_carries_the_output_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_AI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_AI_DEPLOYMENT", "gpt")
    monkeypatch.setenv("AZURE_AI_AUTH_MODE", "api_key")
    monkeypatch.setenv("AZURE_AI_API_KEY", "not-a-real-key")
    default = client_mod.build_chat_model()
    assert default.max_tokens == 1024
    assert default._default_params["max_completion_tokens"] == 1024  # what goes on the wire
    monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", "200")
    assert client_mod.build_chat_model().max_tokens == 200
    assert client_mod.build_chat_model(max_tokens=77).max_tokens == 77  # an explicit override wins


# ── retrieval timeouts ───────────────────────────────────────────────────────


class SlowEmbeddings(FakeEmbeddings):
    async def aembed_query(self, text: str) -> list[float]:
        await asyncio.sleep(5)
        return [0.0, 0.0]


class SlowSearch(FakeSearchClient):
    def search(self, **kwargs):  # type: ignore[override]
        time.sleep(0.4)
        return []


class TestSearchClientBounds:
    """The real ``SearchClient`` is built with the same budget as the asyncio timeout: a search the
    caller gave up on ends soon after instead of holding its worker thread for azure-core's default
    300 s × 10 retries."""

    def _client(self, monkeypatch: pytest.MonkeyPatch, **env: str):
        from app.ai.tools.retrieve import build_search_client

        monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://example.search.windows.net")
        monkeypatch.setenv("AZURE_SEARCH_INDEX", "idx")
        monkeypatch.setenv("AZURE_SEARCH_API_KEY", "not-a-real-key")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        client = build_search_client()
        pipeline = client._client._pipeline
        retry = next(p for p in pipeline._impl_policies if type(p).__name__ == "RetryPolicy")
        return pipeline._transport.connection_config, retry

    def test_defaults_follow_the_retrieval_timeout_setting(self, monkeypatch) -> None:
        config, retry = self._client(monkeypatch)
        assert (config.timeout, config.read_timeout) == (30, 30)
        assert retry.total_retries == 2

    def test_the_timeout_setting_is_used(self, monkeypatch) -> None:
        config, _ = self._client(monkeypatch, AI_RETRIEVAL_TIMEOUT_SECONDS="7")
        assert (config.timeout, config.read_timeout) == (7, 7)


class TestRetrievalTimeouts:
    def test_a_slow_query_embedding_times_out(self) -> None:
        retriever = AzureSearchRetriever(
            FakeSearchClient(), SlowEmbeddings(2), timeout_seconds=0.05
        )
        started = time.perf_counter()
        with pytest.raises(TimeoutError):
            asyncio.run(retriever.retrieve("q"))
        assert time.perf_counter() - started < 2

    def test_a_slow_search_times_out(self) -> None:
        retriever = AzureSearchRetriever(SlowSearch(), FakeEmbeddings(2), timeout_seconds=0.05)

        async def scenario() -> float:
            started = time.perf_counter()
            with pytest.raises(TimeoutError):
                await retriever.retrieve("q")
            return time.perf_counter() - started

        # Timed inside the loop: asyncio.run itself waits for the worker thread at shutdown.
        assert asyncio.run(scenario()) < 0.3  # gave up well before the 0.4 s search ended

    def test_the_timeout_comes_from_the_setting_when_none_is_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", "0.05")
        retriever = AzureSearchRetriever(FakeSearchClient(), SlowEmbeddings(2))
        with pytest.raises(TimeoutError):
            asyncio.run(retriever.retrieve("q"))

    def test_a_fast_retrieval_is_unaffected(self) -> None:
        rows = [{"id": "a", "content": "text", "source": "docs/a.md", "title": "A"}]
        retriever = AzureSearchRetriever(
            FakeSearchClient(rows), FakeEmbeddings(2), timeout_seconds=5
        )
        chunks = asyncio.run(retriever.retrieve("q"))
        assert chunks[0]["source"] == "docs/a.md"

    def test_each_step_has_its_own_budget(self) -> None:
        # embedding takes ~0.06 s and the search ~0.06 s: 0.1 s each is enough, 0.12 s in total
        class Mid(FakeEmbeddings):
            async def aembed_query(self, text: str) -> list[float]:
                await asyncio.sleep(0.06)
                return [0.0, 0.0]

        class MidSearch(FakeSearchClient):
            def search(self, **kwargs):  # type: ignore[override]
                time.sleep(0.06)
                return []

        retriever = AzureSearchRetriever(MidSearch(), Mid(2), timeout_seconds=0.1)
        assert asyncio.run(retriever.retrieve("q")) == []
