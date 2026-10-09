"""``POST /v1/admin/ingest`` (api roadmap 3.3, ADR-0015) — offline.

The key guard (fails closed, constant-time, never logged), the one-run-at-a-time lock, the
background run and its bounded outcome (log + event, never an exception's text), the corpus size
cap (threat model G7) and the redaction of the key's names.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

import app.admin as admin_mod
import app.ai.client as client_mod
import app.events as events_mod
from app.admin import ADMIN_KEY_MIN_LENGTH
from app.ai import ingest, ingest_job
from app.ai.config import (
    DEFAULT_INGEST_MAX_BYTES,
    DEFAULT_INGEST_MAX_FILES,
    AINotConfigured,
    get_ai_settings,
)
from app.ai.ingest import (
    CorpusEmpty,
    CorpusNotFound,
    CorpusTooLarge,
    CorpusUnreadable,
    EmbeddingDimensionMismatch,
    IndexDimensionMismatch,
    IndexHasNoVectorField,
    IngestError,
    IngestReport,
    IngestTimeout,
)
from app.logging_config import REDACTION_CENSOR, configure_logging, get_logger
from app.main import app

GOOD_KEY = "k" * 40
TRACE = "0c70" + "a" * 28


def _json_lines(out: str) -> list[dict[str, Any]]:
    lines = []
    for line in out.splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            continue
    return lines


class _Pending:
    """Stands in for a run that has not finished."""

    def done(self) -> bool:
        return False


def _rebind_module_loggers() -> None:
    """structlog caches a logger on its first use with the configuration of that moment, so a
    module that first logged during these tests would keep their (synchronous, stdout) setup for
    the rest of the session. Every ``app.*`` module's ``_log`` is bound afresh, from its name."""
    for name, module in list(sys.modules.items()):
        proxy = getattr(module, "_log", None)
        args = getattr(proxy, "_logger_factory_args", None)
        if name.startswith("app.") and args:
            module._log = get_logger(*args)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("INGEST_ADMIN_KEY", "INGEST_MAX_FILES", "INGEST_MAX_BYTES", "CORPUS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_AI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_AI_EMBEDDING_DEPLOYMENT", "embed")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.invalid")
    monkeypatch.setenv("AZURE_SEARCH_INDEX", "docs")
    saved = structlog.get_config()
    configure_logging(async_sink=False)
    admin_mod._reset_warnings_for_tests()
    ingest_job._reset_for_tests()
    yield
    ingest_job._reset_for_tests()
    admin_mod._reset_warnings_for_tests()
    structlog.configure(**saved)
    _rebind_module_loggers()


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("INGEST_ADMIN_KEY", GOOD_KEY)
    return GOOD_KEY


class _Runs:
    """A fake ``ingest_corpus`` that records its calls and finishes when told to."""

    def __init__(self) -> None:
        self.calls = 0
        self.gate = threading.Event()
        self.gate.set()

    async def __call__(self, **kwargs: Any) -> IngestReport:
        self.calls += 1
        while not self.gate.is_set():
            await asyncio.sleep(0.005)
        return IngestReport(documents=2, chunks=5, uploaded=5, seconds=0.1)


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> _Runs:
    fake = _Runs()
    monkeypatch.setattr(ingest_job, "ingest_corpus", fake)
    return fake


def _wait(predicate: Any, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


def _assert_error(res: Any, status: int, code: str) -> None:
    assert res.status_code == status
    body = res.json()
    assert body["error"] == code
    assert body["trace_id"] == res.headers["x-trace-id"]


# ── the guard ────────────────────────────────────────────────────────────────


class TestKeyGuard:
    def test_the_right_key_starts_a_run(self, key: str, runs: _Runs) -> None:
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers={"x-admin-key": key, "x-trace-id": TRACE})
            assert res.status_code == 202
            assert res.json() == {"status": "started"}
            assert res.headers["x-trace-id"] == TRACE
            _wait(lambda: runs.calls == 1)
            _wait(lambda: not ingest_job.is_running())

    @pytest.mark.parametrize(
        "headers",
        [{}, {"x-admin-key": ""}, {"x-admin-key": "wrong"}, {"x-admin-key": "k" * 39 + "x"}],
        ids=["no header", "empty", "wrong", "wrong, same length"],
    )
    def test_a_missing_or_wrong_key_is_401(
        self, key: str, runs: _Runs, headers: dict[str, str]
    ) -> None:
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers=headers)
        _assert_error(res, 401, "unauthorized")
        assert runs.calls == 0

    def test_a_longer_key_with_the_right_prefix_is_401(self, key: str, runs: _Runs) -> None:
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers={"x-admin-key": key + "k"})
        _assert_error(res, 401, "unauthorized")

    def test_a_key_that_is_not_ascii_is_401_not_a_crash(self, key: str, runs: _Runs) -> None:
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers={"x-admin-key": "é".encode()})
        _assert_error(res, 401, "unauthorized")

    def test_the_header_name_is_case_insensitive(self, key: str, runs: _Runs) -> None:
        with TestClient(app) as client:
            assert client.post("/v1/admin/ingest", headers={"X-Admin-Key": key}).status_code == 202

    def test_no_key_configured_fails_closed_whatever_is_sent(self, runs: _Runs) -> None:
        with TestClient(app) as client:
            for sent in ({}, {"x-admin-key": ""}, {"x-admin-key": "anything"}):
                _assert_error(client.post("/v1/admin/ingest", headers=sent), 401, "unauthorized")
        assert runs.calls == 0

    def test_a_blank_key_is_not_a_key(self, monkeypatch: pytest.MonkeyPatch, runs: _Runs) -> None:
        monkeypatch.setenv("INGEST_ADMIN_KEY", "   ")
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers={"x-admin-key": "   "})
        _assert_error(res, 401, "unauthorized")

    def test_a_short_key_is_treated_as_not_set(
        self, monkeypatch: pytest.MonkeyPatch, runs: _Runs
    ) -> None:
        short = "s" * (ADMIN_KEY_MIN_LENGTH - 1)
        monkeypatch.setenv("INGEST_ADMIN_KEY", short)
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers={"x-admin-key": short})
        _assert_error(res, 401, "unauthorized")
        assert runs.calls == 0

    def test_a_key_of_exactly_the_minimum_length_works(
        self, monkeypatch: pytest.MonkeyPatch, runs: _Runs
    ) -> None:
        exact = "e" * ADMIN_KEY_MIN_LENGTH
        monkeypatch.setenv("INGEST_ADMIN_KEY", exact)
        with TestClient(app) as client:
            assert (
                client.post("/v1/admin/ingest", headers={"x-admin-key": exact}).status_code == 202
            )

    def test_the_key_is_compared_in_constant_time(
        self, key: str, runs: _Runs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[bytes, bytes]] = []
        real = admin_mod.hmac.compare_digest

        def spy(a: Any, b: Any) -> bool:
            seen.append((a, b))
            return real(a, b)

        monkeypatch.setattr(admin_mod.hmac, "compare_digest", spy)
        with TestClient(app) as client:
            client.post("/v1/admin/ingest", headers={"x-admin-key": "nope"})
        assert len(seen) == 1
        # hashed first: the two sides are the same length, so a different length shows nothing
        assert len(seen[0][0]) == len(seen[0][1]) == 32

    def test_the_guard_runs_before_anything_else(
        self, monkeypatch: pytest.MonkeyPatch, runs: _Runs
    ) -> None:
        # No search configured, a run in flight: a caller without the key still only learns "401".
        monkeypatch.delenv("AZURE_SEARCH_ENDPOINT")
        monkeypatch.setenv("INGEST_ADMIN_KEY", GOOD_KEY)
        ingest_job._task = _Pending()  # type: ignore[assignment]
        with TestClient(app) as client:
            _assert_error(client.post("/v1/admin/ingest"), 401, "unauthorized")

    def test_the_route_exists_only_under_v1_and_only_as_post(self, key: str) -> None:
        with TestClient(app) as client:
            assert client.post("/admin/ingest", headers={"x-admin-key": key}).status_code == 404
            assert client.get("/v1/admin/ingest", headers={"x-admin-key": key}).status_code == 405


class TestKeyIsNeverLogged:
    def test_a_refusal_logs_the_reason_kind_once_and_never_the_key_or_its_length(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        short = "S3cretShortKey"
        monkeypatch.setenv("INGEST_ADMIN_KEY", short)
        with TestClient(app) as client:
            for _ in range(3):
                client.post("/v1/admin/ingest", headers={"x-admin-key": short})
        out = capsys.readouterr().out
        warnings = [r for r in _json_lines(out) if r.get("level") == "warning"]
        usable = [r for r in warnings if "admin key is not usable" in r["message"]]
        assert len(usable) == 1
        assert usable[0]["reason"] == "too_short"
        assert short not in out
        standard = {
            "level",
            "service",
            "origin",
            "env",
            "timestamp",
            "message",
            "trace_id",
            "reason",
        }
        assert set(usable[0]) <= standard
        # nothing but the reason kind says anything about the key: no field holds its length
        assert str(len(short)) not in [str(v) for k, v in usable[0].items() if k != "timestamp"]

    def test_an_unset_key_warns_once_with_its_own_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TestClient(app) as client:
            client.post("/v1/admin/ingest")
            client.post("/v1/admin/ingest")
        usable = [
            r
            for r in _json_lines(capsys.readouterr().out)
            if "admin key is not usable" in r.get("message", "")
        ]
        assert [r["reason"] for r in usable] == ["not_set"]

    def test_wrong_and_missing_keys_log_a_reason_and_not_the_value(
        self, key: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TestClient(app) as client:
            client.post("/v1/admin/ingest", headers={"x-admin-key": "guess-guess-guess"})
            client.post("/v1/admin/ingest")
        out = capsys.readouterr().out
        reasons = [
            r["reason"] for r in _json_lines(out) if r.get("message") == "admin call refused"
        ]
        assert reasons == ["wrong_key", "no_key"]
        assert "guess-guess-guess" not in out
        assert key not in out

    def test_a_successful_call_does_not_log_the_key(
        self, key: str, runs: _Runs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TestClient(app) as client:
            client.post("/v1/admin/ingest", headers={"x-admin-key": key})
            _wait(lambda: runs.calls == 1)
            _wait(lambda: not ingest_job.is_running())
        assert key not in capsys.readouterr().out

    @pytest.mark.parametrize(
        "field", ["x-admin-key", "X-Admin-Key", "x_admin_key", "admin_key", "ingest_admin_key"]
    )
    def test_the_names_of_the_key_are_redacted_in_a_log_line(
        self, field: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        get_logger("app.test").info("probe", **{field.replace("-", "_"): "the-secret-value"})
        get_logger("app.test").info("nested", headers={field: "the-secret-value"})
        out = capsys.readouterr().out
        assert "the-secret-value" not in out
        assert out.count(REDACTION_CENSOR) == 2


# ── the lock ─────────────────────────────────────────────────────────────────


class TestOneRunAtATime:
    def test_a_second_call_while_one_runs_is_409_and_a_third_after_it_ends_is_202(
        self, key: str, runs: _Runs
    ) -> None:
        runs.gate.clear()
        with TestClient(app) as client:
            headers = {"x-admin-key": key}
            assert client.post("/v1/admin/ingest", headers=headers).status_code == 202
            _wait(lambda: runs.calls == 1)
            second = client.post("/v1/admin/ingest", headers={**headers, "x-trace-id": TRACE})
            _assert_error(second, 409, "ingest_running")
            assert second.headers["x-trace-id"] == TRACE
            assert runs.calls == 1  # the refused call started nothing

            runs.gate.set()
            _wait(lambda: not ingest_job.is_running())
            assert client.post("/v1/admin/ingest", headers=headers).status_code == 202
            _wait(lambda: runs.calls == 2)

    def test_a_failed_run_frees_the_lock(self, key: str, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        async def fails(**kwargs: Any) -> IngestReport:
            calls["n"] += 1
            raise RuntimeError("boom")

        monkeypatch.setattr(ingest_job, "ingest_corpus", fails)
        with TestClient(app) as client:
            headers = {"x-admin-key": key}
            assert client.post("/v1/admin/ingest", headers=headers).status_code == 202
            _wait(lambda: calls["n"] == 1)
            _wait(lambda: not ingest_job.is_running())
            assert client.post("/v1/admin/ingest", headers=headers).status_code == 202
            _wait(lambda: calls["n"] == 2)


class TestTraceIdReachesTheBackgroundRun:
    def test_the_outcome_line_carries_the_trace_id_of_the_post(
        self, key: str, runs: _Runs, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TestClient(app) as client:
            client.post("/v1/admin/ingest", headers={"x-admin-key": key, "x-trace-id": TRACE})
            _wait(lambda: runs.calls == 1)
            _wait(lambda: not ingest_job.is_running())
        done = [
            r for r in _json_lines(capsys.readouterr().out) if r.get("message") == "ingest finished"
        ]
        assert len(done) == 1 and done[0]["trace_id"] == TRACE


class TestRefusalLogVolume:
    def _refusals(self, out: str) -> list[dict[str, Any]]:
        return [r for r in _json_lines(out) if r.get("message") == "admin call refused"]

    def test_a_caller_without_the_key_cannot_write_a_line_per_request(
        self, key: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 1000.0}
        monkeypatch.setattr(admin_mod.time, "monotonic", lambda: clock["now"])
        with TestClient(app) as client:
            for _ in range(50):
                _assert_error(client.post("/v1/admin/ingest"), 401, "unauthorized")
            assert len(self._refusals(capsys.readouterr().out)) == 1
            # after the interval the next one is logged, with how many were left out
            clock["now"] += admin_mod.REFUSAL_LOG_INTERVAL_SECONDS + 1
            client.post("/v1/admin/ingest")
        later = self._refusals(capsys.readouterr().out)
        assert len(later) == 1 and later[0]["also_refused"] == 49

    def test_each_reason_has_its_own_budget(
        self, key: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TestClient(app) as client:
            for _ in range(5):
                client.post("/v1/admin/ingest")
                client.post("/v1/admin/ingest", headers={"x-admin-key": "wrong"})
        reasons = [r["reason"] for r in self._refusals(capsys.readouterr().out)]
        assert sorted(reasons) == ["no_key", "wrong_key"]


class TestNotConfigured:
    def test_no_search_is_503_ai_unavailable_and_starts_nothing(
        self, key: str, runs: _Runs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AZURE_SEARCH_ENDPOINT")
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers={"x-admin-key": key})
        _assert_error(res, 503, "ai_unavailable")
        assert runs.calls == 0 and not ingest_job.is_running()

    def test_no_embedding_deployment_is_503(
        self, key: str, runs: _Runs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AZURE_AI_EMBEDDING_DEPLOYMENT")
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers={"x-admin-key": key})
        _assert_error(res, 503, "ai_unavailable")


# ── the outcome ──────────────────────────────────────────────────────────────


class _Telemetry(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Telemetry]:
    monkeypatch.setattr(
        events_mod, "get_observability_state", lambda: {"enabled": True, "reason": "ok"}
    )
    handler = _Telemetry()
    logger = logging.getLogger("app.telemetry")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(level)


def _run_job(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    async def fake(**kwargs: Any) -> IngestReport:
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(ingest_job, "ingest_corpus", fake)
    asyncio.run(ingest_job._execute())


def _event(telemetry: _Telemetry) -> dict[str, Any]:
    events = [r for r in telemetry.records if r.getMessage() == ingest_job.EVENT_NAME]
    assert len(events) == 1
    return events[0].__dict__


class TestOutcome:
    def test_a_good_run_is_logged_and_emitted_as_counts_and_seconds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        telemetry: _Telemetry,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        report = IngestReport(documents=3, chunks=9, uploaded=9, removed=2, seconds=1.5)
        _run_job(monkeypatch, report)
        line = next(
            r for r in _json_lines(capsys.readouterr().out) if r.get("message") == "ingest finished"
        )
        assert line["outcome"] == "success" and line["error_kind"] == "none"
        assert (line["documents"], line["chunks"], line["uploaded"], line["failed"]) == (3, 9, 9, 0)
        assert line["removed"] == 2 and "seconds" in line
        event = _event(telemetry)
        assert event[events_mod.CUSTOM_EVENT_MARKER] == "ingest.completed"
        assert event["outcome"] == "success" and event["error_kind"] == "none"
        assert event["documents"] == 3 and event["chunks"] == 9 and event["uploaded"] == 9

    @pytest.mark.parametrize(
        "report",
        [
            IngestReport(documents=1, chunks=2, uploaded=1, failed=1),
            IngestReport(documents=1, chunks=2, uploaded=2, removal_failed=1),
            IngestReport(documents=1, chunks=2, uploaded=2, removal_refused=True),
        ],
        ids=["failed uploads", "failed removals", "removal refused"],
    )
    def test_a_run_that_finished_with_problems_is_partial(
        self, monkeypatch: pytest.MonkeyPatch, telemetry: _Telemetry, report: IngestReport
    ) -> None:
        _run_job(monkeypatch, report)
        assert _event(telemetry)["outcome"] == "partial"

    @pytest.mark.parametrize(
        ("exc", "kind"),
        [
            (CorpusNotFound("x"), "corpus_not_found"),
            (CorpusEmpty("x"), "corpus_empty"),
            (CorpusTooLarge(2, 10, 1, 5), "corpus_too_large"),
            (CorpusUnreadable("x"), "corpus_unreadable"),
            (IngestTimeout("a step", 30.0), "timeout"),
            (IndexDimensionMismatch("i", 1, 2), "dimension_mismatch"),
            (EmbeddingDimensionMismatch(1, 2), "dimension_mismatch"),
            (IndexHasNoVectorField("i"), "index_shape"),
            (AINotConfigured("x"), "not_configured"),
            (IngestError("x"), "ingest_error"),
            (RuntimeError("x"), "unexpected"),
            (ValueError("x"), "unexpected"),
        ],
    )
    def test_a_failure_is_a_bounded_kind(
        self, monkeypatch: pytest.MonkeyPatch, telemetry: _Telemetry, exc: BaseException, kind: str
    ) -> None:
        assert ingest_job.error_kind(exc) == kind
        _run_job(monkeypatch, exc)
        event = _event(telemetry)
        assert event["outcome"] == "error" and event["error_kind"] == kind

    def test_a_cancellation_has_its_own_kind(self) -> None:
        assert ingest_job.error_kind(asyncio.CancelledError()) == "cancelled"

    def test_an_unexpected_failure_never_shows_its_message_or_a_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        telemetry: _Telemetry,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        secret = "https://search.internal.example/indexes/docs?api-key=SECRET-123 body=<html>"
        _run_job(monkeypatch, RuntimeError(secret))
        out = capsys.readouterr().out
        assert "SECRET-123" not in out and "search.internal.example" not in out
        assert "Traceback" not in out and "exc_info" not in out
        for record in telemetry.records:
            assert "SECRET-123" not in json.dumps(record.__dict__, default=str)
        failed = next(r for r in _json_lines(out) if r.get("message") == "ingest failed")
        assert failed["error_kind"] == "unexpected"

    def test_a_known_failure_is_logged_by_kind_not_by_its_sentence(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run_job(monkeypatch, CorpusUnreadable("private/notes.md is not valid UTF-8 text"))
        out = capsys.readouterr().out
        assert "private/notes.md" not in out
        assert "corpus_unreadable" in out

    def test_the_run_never_raises_into_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _run_job(monkeypatch, RuntimeError("boom"))  # returns normally

    def test_a_cancelled_run_is_reported_and_frees_the_lock(
        self, monkeypatch: pytest.MonkeyPatch, telemetry: _Telemetry
    ) -> None:
        async def go() -> None:
            async def hangs(**kwargs: Any) -> IngestReport:
                await asyncio.sleep(60)
                return IngestReport()

            monkeypatch.setattr(ingest_job, "ingest_corpus", hangs)
            ingest_job.start()
            assert ingest_job.is_running()
            await asyncio.sleep(0)
            task = ingest_job._task
            assert task is not None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not ingest_job.is_running()

        asyncio.run(go())
        event = _event(telemetry)
        assert event["outcome"] == "error" and event["error_kind"] == "cancelled"

    def test_the_event_has_no_document_text_or_path_attributes(
        self, monkeypatch: pytest.MonkeyPatch, telemetry: _Telemetry
    ) -> None:
        _run_job(monkeypatch, IngestReport(documents=1, chunks=1, uploaded=1))
        extra = {
            k
            for k in _event(telemetry)
            if k not in logging.makeLogRecord({}).__dict__ and k != events_mod.CUSTOM_EVENT_MARKER
        }
        assert extra <= {
            "outcome",
            "error_kind",
            "documents",
            "chunks",
            "uploaded",
            "failed",
            "removed",
            "removal_failed",
            "removal_refused",
            "seconds",
            "taskName",
            "message",
        }


# ── the corpus size cap ──────────────────────────────────────────────────────


def _corpus(root: Path, files: int, size: int = 20) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (root / f"f{i}.md").write_text("# T\n\n" + "x" * size, encoding="utf-8")
    return root


class TestOffTheEventLoop:
    def test_the_corpus_scan_runs_in_a_worker_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, int] = {}
        real = ingest.scan_corpus

        def spy(root: Path, **kwargs: Any) -> Any:
            seen["thread"] = threading.get_ident()
            return real(root, **kwargs)

        monkeypatch.setattr(ingest, "scan_corpus", spy)
        monkeypatch.setenv("CORPUS_DIR", str(_corpus(tmp_path / "c", 1)))

        def stop(*a: Any, **k: Any) -> None:
            raise AINotConfigured("stop here")

        monkeypatch.setattr(client_mod, "get_embeddings", stop)
        with pytest.raises(AINotConfigured):
            asyncio.run(ingest.ingest_corpus())
        assert seen["thread"] != threading.get_ident()

    def test_the_index_setup_runs_in_a_worker_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, int] = {}

        def spy(settings: Any, index: Any) -> None:
            seen["thread"] = threading.get_ident()
            raise CorpusEmpty("stop here")

        async def fine(*a: Any, **k: Any) -> None:
            return None

        monkeypatch.setenv("CORPUS_DIR", str(_corpus(tmp_path / "c", 1)))
        monkeypatch.setattr(client_mod, "get_embeddings", lambda: object())
        monkeypatch.setattr(ingest, "check_embedding_dimensions", fine)
        monkeypatch.setattr(ingest, "_ensure_index_blocking", spy)
        with pytest.raises(CorpusEmpty):
            asyncio.run(ingest.ingest_corpus())
        assert seen["thread"] != threading.get_ident()


class TestReportersCannotEscape:
    def test_a_reporter_that_fails_does_not_end_the_task_with_an_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any) -> None:
            raise RuntimeError("secret-text-from-a-reporter")

        monkeypatch.setattr(ingest_job, "_report_success", boom)
        monkeypatch.setattr(ingest_job, "_report_failure", boom)
        _run_job(monkeypatch, IngestReport())  # returns normally
        _run_job(monkeypatch, RuntimeError("x"))  # returns normally


class TestCorpusCap:
    def test_the_defaults_are_a_thousand_files_and_fifty_mebibytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (DEFAULT_INGEST_MAX_FILES, DEFAULT_INGEST_MAX_BYTES) == (1000, 50 * 1024 * 1024)
        settings = get_ai_settings()
        assert (settings.ingest_max_files, settings.ingest_max_bytes) == (1000, 52_428_800)

    def test_the_cap_is_configurable_and_a_mistake_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INGEST_MAX_FILES", "5")
        monkeypatch.setenv("INGEST_MAX_BYTES", "1000")
        settings = get_ai_settings()
        assert (settings.ingest_max_files, settings.ingest_max_bytes) == (5, 1000)
        for bad in ("many", "0", "-1", ""):
            monkeypatch.setenv("INGEST_MAX_FILES", bad)
            monkeypatch.setenv("INGEST_MAX_BYTES", bad)
            settings = get_ai_settings()
            assert (settings.ingest_max_files, settings.ingest_max_bytes) == (1000, 52_428_800)

    def test_too_many_files_refuses_before_any_file_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _corpus(tmp_path / "c", 4)

        def not_allowed(*a: Any, **k: Any) -> str:
            raise AssertionError("a file was read")

        monkeypatch.setattr(Path, "read_text", not_allowed)
        with pytest.raises(CorpusTooLarge) as caught:
            ingest.scan_corpus(root, max_files=3)
        assert (caught.value.files, caught.value.max_files) == (4, 3)

    def test_too_many_bytes_refuses_before_any_file_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _corpus(tmp_path / "c", 3, size=100)
        monkeypatch.setattr(
            Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
        )
        with pytest.raises(CorpusTooLarge) as caught:
            ingest.scan_corpus(root, max_bytes=300)
        assert caught.value.size > 300 and caught.value.max_bytes == 300

    def test_a_corpus_exactly_at_the_cap_is_accepted(self, tmp_path: Path) -> None:
        root = _corpus(tmp_path / "c", 3, size=10)
        total = sum(p.stat().st_size for p in root.glob("*.md"))
        load = ingest.scan_corpus(root, max_files=3, max_bytes=total)
        assert len(load.documents) == 3

    def test_only_markdown_files_count(self, tmp_path: Path) -> None:
        root = _corpus(tmp_path / "c", 2)
        for i in range(10):
            (root / f"image{i}.png").write_bytes(b"\x00" * 1000)
        assert len(ingest.scan_corpus(root, max_files=2, max_bytes=200).documents) == 2

    def test_without_a_cap_nothing_is_refused(self, tmp_path: Path) -> None:
        assert len(ingest.scan_corpus(_corpus(tmp_path / "c", 6)).documents) == 6

    def test_the_reason_is_one_line_of_numbers_and_names_the_settings(self, tmp_path: Path) -> None:
        root = _corpus(tmp_path / "c", 4)
        with pytest.raises(CorpusTooLarge) as caught:
            ingest.scan_corpus(root, max_files=3)
        message = str(caught.value)
        assert "\n" not in message
        assert "INGEST_MAX_FILES" in message and "INGEST_MAX_BYTES" not in message
        assert str(root) not in message and "f0.md" not in message

    def test_a_byte_cap_alone_does_not_claim_a_file_cap(self, tmp_path: Path) -> None:
        root = _corpus(tmp_path / "c", 3, size=100)
        with pytest.raises(CorpusTooLarge) as caught:
            ingest.scan_corpus(root, max_bytes=300)
        message = str(caught.value)
        assert "INGEST_MAX_BYTES" in message and "INGEST_MAX_FILES" not in message

    def test_a_run_over_the_cap_embeds_nothing_and_touches_no_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CORPUS_DIR", str(_corpus(tmp_path / "c", 3)))
        monkeypatch.setenv("INGEST_MAX_FILES", "2")

        def forbidden(*a: Any, **k: Any) -> None:
            raise AssertionError("the run went on past the cap")

        monkeypatch.setattr(client_mod, "get_embeddings", forbidden)
        monkeypatch.setattr(ingest, "build_index_client", forbidden)
        monkeypatch.setattr(ingest, "build_search_client", forbidden)
        with pytest.raises(CorpusTooLarge):
            asyncio.run(ingest.ingest_corpus())

    def test_the_command_line_stops_with_the_one_line_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CORPUS_DIR", str(_corpus(tmp_path / "c", 3)))
        monkeypatch.setenv("INGEST_MAX_FILES", "2")
        monkeypatch.setattr("sys.argv", ["ingest"])
        with pytest.raises(SystemExit) as caught:
            ingest.main()
        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert err.startswith("ingest stopped:") and "INGEST_MAX_FILES" in err
        assert "Traceback" not in err
