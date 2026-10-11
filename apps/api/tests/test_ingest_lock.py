"""The ingest lock shared by every replica (api roadmap 5.2): the statements, the protocol, the job.

Offline: a dict plays the database table with the same rules the SQL has (one row per name, a
take-over only when ``expires_at`` has passed, a renewal or release only by the holder), and a
clock the test moves plays the database's ``SYSUTCDATETIME()``. The statements themselves are
compiled for SQL Server and read. A test waits until the thing it is about has happened (with a
generous limit), never for a fixed time.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mssql
from sqlalchemy.exc import IntegrityError, OperationalError

import app.ai.ingest_job as job_mod
import app.ai.ingest_lock as lock_mod
from app.ai import ingest_job
from app.ai.ingest import IngestReport
from app.ai.ingest_lock import (
    DEFAULT_TTL_SECONDS,
    DatabaseIngestLock,
    IngestLockUnavailable,
    database_is_configured,
)
from app.config import DEFAULT_DATABASE_URL
from app.errors import Conflict
from app.main import app
from app.repositories import ingest_lock as repo

KEY = "k" * 40
REAL_URL = (
    "mssql+aioodbc://u:p@real-server.database.windows.net:1433/app"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
)
HANG = "hang"  # a renewal answer that never comes


async def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.005)


def _wait(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the condition"
        time.sleep(0.01)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=mssql.dialect()))


# ── the statements ───────────────────────────────────────────────────────────

HOLDER = UUID("11111111-1111-4111-8111-111111111111")


class TestStatements:
    def test_taking_over_updates_only_an_expired_row_by_the_database_clock(self) -> None:
        sql = _sql(repo.take_over_expired_statement(HOLDER, 300))
        assert sql.startswith("UPDATE ingest_locks SET ")
        assert "ingest_locks.expires_at < sysutcdatetime()" in sql
        assert "acquired_at=sysutcdatetime()" in sql
        assert "expires_at=dateadd(SECOND, " in sql

    def test_inserting_sets_both_instants_by_the_database_clock(self) -> None:
        sql = _sql(repo.insert_statement(HOLDER, 300))
        assert sql.startswith("INSERT INTO ingest_locks (name, holder, acquired_at, expires_at)")
        assert sql.count("sysutcdatetime()") == 2 and "dateadd(SECOND, " in sql

    def test_renewing_is_by_the_holder_alone(self) -> None:
        sql = _sql(repo.renew_statement(HOLDER, 300))
        assert "ingest_locks.name = " in sql and "ingest_locks.holder = " in sql
        assert "expires_at < " not in sql
        assert "expires_at=dateadd(SECOND, " in sql

    def test_releasing_is_by_the_holder_alone(self) -> None:
        sql = _sql(repo.release_statement(HOLDER))
        assert sql.startswith("DELETE FROM ingest_locks WHERE ingest_locks.name = ")
        assert "AND ingest_locks.holder = " in sql

    def test_the_existence_check_reads_the_row_by_name_only(self) -> None:
        sql = _sql(repo.exists_statement())
        assert sql.startswith("SELECT ingest_locks.name") and "WHERE ingest_locks.name = " in sql

    @pytest.mark.parametrize(
        "statement",
        [
            repo.take_over_expired_statement(HOLDER, 300),
            repo.insert_statement(HOLDER, 300),
            repo.renew_statement(HOLDER, 300),
            repo.release_statement(HOLDER),
        ],
    )
    def test_no_statement_carries_a_time_from_this_machine(self, statement: Any) -> None:
        params = statement.compile(dialect=mssql.dialect()).params
        assert not [v for v in params.values() if isinstance(v, datetime)]

    def test_the_ttl_is_a_bound_parameter_never_text(self) -> None:
        compiled = repo.take_over_expired_statement(HOLDER, 123).compile(dialect=mssql.dialect())
        assert 123 in compiled.params.values()
        assert str(HOLDER) not in str(compiled)


class _Result:
    def __init__(self, rowcount: int, rows: list[Any]) -> None:
        self.rowcount = rowcount
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rowcount: int, rows: list[Any] | None = None) -> None:
        self.rowcount = rowcount
        self.rows = rows or []
        self.executed: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.executed.append(statement)
        return _Result(self.rowcount, self.rows)


class TestRepositoryFunctions:
    @pytest.mark.parametrize(("rows", "expected"), [(1, True), (0, False)])
    def test_take_over_renew_and_release_say_whether_a_row_changed(
        self, rows: int, expected: bool
    ) -> None:
        for call in (
            lambda s: repo.take_over_expired(s, HOLDER, 300),
            lambda s: repo.renew(s, HOLDER, 300),
            lambda s: repo.release(s, HOLDER),
        ):
            session = _Session(rows)
            assert asyncio.run(call(session)) is expected
            assert len(session.executed) == 1

    def test_inserting_executes_one_statement_and_does_not_commit(self) -> None:
        session = _Session(1)
        asyncio.run(repo.insert_lock(session, HOLDER, 300))  # type: ignore[arg-type]
        assert len(session.executed) == 1 and not hasattr(session, "commit")

    def test_the_existence_check_says_whether_the_row_is_there(self) -> None:
        assert asyncio.run(repo.lock_exists(_Session(0, [("ingest",)]))) is True  # type: ignore[arg-type]
        assert asyncio.run(repo.lock_exists(_Session(0, []))) is False  # type: ignore[arg-type]


# ── a database the tests can read ─────────────────────────────────────────────


class FakeDatabase:
    """The ``ingest_locks`` table and the database's clock, with the SQL's rules."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.rows: dict[str, tuple[UUID, float]] = {}  # name -> (holder, expires_at)
        self.down = False
        self.errors: list[Exception] = []  # raised by the next calls, one each
        self.hang: set[str] = set()  # operations that never answer
        self.phantom_integrity_error = False  # an INSERT that fails with no row behind it
        self.commit_started = threading.Event()
        self.commit_gate: asyncio.Event | None = None
        self.commits = 0
        self.rollbacks = 0

    async def _check(self, operation: str) -> None:
        if operation in self.hang:
            await asyncio.sleep(3600)
        if self.down:
            raise OperationalError("SELECT 1", {}, Exception("connection reset by peer"))
        if self.errors:
            raise self.errors.pop(0)

    async def take_over_expired(
        self, session: Any, holder: UUID, ttl: float, name: str = "ingest"
    ) -> bool:
        await self._check("take_over")
        row = self.rows.get(name)
        if row is not None and row[1] < self.now:
            self.rows[name] = (holder, self.now + ttl)
            return True
        return False

    async def insert_lock(
        self, session: Any, holder: UUID, ttl: float, name: str = "ingest"
    ) -> None:
        await self._check("insert")
        if self.phantom_integrity_error:
            raise IntegrityError("INSERT", {}, Exception("not null violation"))
        if name in self.rows:
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        self.rows[name] = (holder, self.now + ttl)

    async def lock_exists(self, session: Any, name: str = "ingest") -> bool:
        await self._check("exists")
        return name in self.rows and not self.phantom_integrity_error

    async def renew(self, session: Any, holder: UUID, ttl: float, name: str = "ingest") -> bool:
        await self._check("renew")
        row = self.rows.get(name)
        if row is not None and row[0] == holder:
            self.rows[name] = (holder, self.now + ttl)
            return True
        return False

    async def release(self, session: Any, holder: UUID, name: str = "ingest") -> bool:
        await self._check("release")
        row = self.rows.get(name)
        if row is not None and row[0] == holder:
            del self.rows[name]
            return True
        return False


class _FakeSession:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def commit(self) -> None:
        self.db.commit_started.set()
        if self.db.commit_gate is not None:
            await self.db.commit_gate.wait()
        await self.db._check("commit")
        self.db.commits += 1

    async def rollback(self) -> None:
        self.db.rollbacks += 1


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    database = FakeDatabase()

    @asynccontextmanager
    async def session_scope():
        yield _FakeSession(database)

    monkeypatch.setattr(lock_mod, "get_sessionmaker", lambda: session_scope)
    for name in ("take_over_expired", "insert_lock", "lock_exists", "renew", "release"):
        monkeypatch.setattr(repo, name, getattr(database, name))
    return database


def _acquire(lock: DatabaseIngestLock) -> bool:
    return asyncio.run(lock.acquire())


class Recorder:
    """Stands in for a structlog logger; keeps what was logged."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def _add(self, level: str, message: str, **fields: Any) -> None:
        self.lines.append({"level": level, "message": message, **fields})

    def info(self, message: str, **fields: Any) -> None:
        self._add("info", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._add("warning", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self._add("error", message, **fields)


# ── the protocol, as two replicas see it ──────────────────────────────────────


class TestTwoReplicas:
    def test_the_first_to_ask_gets_it_and_the_second_does_not(self, db: FakeDatabase) -> None:
        a, b = DatabaseIngestLock(), DatabaseIngestLock()
        assert _acquire(a) is True
        assert _acquire(b) is False
        assert a.holder is not None and b.holder is None
        assert db.rollbacks == 1  # the failed insert is rolled back, not left open

    def test_it_is_free_again_when_the_holder_releases(self, db: FakeDatabase) -> None:
        a, b = DatabaseIngestLock(), DatabaseIngestLock()
        assert _acquire(a)
        asyncio.run(a.release())
        assert db.rows == {} and a.holder is None
        assert _acquire(b) is True

    def test_a_lock_left_by_a_crashed_run_expires_on_its_own(self, db: FakeDatabase) -> None:
        a, b = DatabaseIngestLock(ttl_seconds=300), DatabaseIngestLock(ttl_seconds=300)
        assert _acquire(a)  # ... and the replica dies: nothing renews or releases it
        db.now += 299
        assert _acquire(b) is False
        db.now += 2
        assert _acquire(b) is True
        assert db.rows["ingest"][0] == b.holder

    def test_a_renewed_lock_does_not_expire(self, db: FakeDatabase) -> None:
        a, b = DatabaseIngestLock(ttl_seconds=300), DatabaseIngestLock(ttl_seconds=300)
        assert _acquire(a)
        for _ in range(5):
            db.now += 200
            assert asyncio.run(a.renew()) is True
            assert _acquire(b) is False

    def test_a_holder_that_was_taken_over_learns_it_on_its_next_renewal(
        self, db: FakeDatabase
    ) -> None:
        a, b = DatabaseIngestLock(ttl_seconds=300), DatabaseIngestLock(ttl_seconds=300)
        assert _acquire(a)
        db.now += 400
        assert _acquire(b)
        assert asyncio.run(a.renew()) is False
        asyncio.run(a.release())  # its release must not remove b's lock
        assert db.rows["ingest"][0] == b.holder

    def test_a_second_release_is_harmless(self, db: FakeDatabase) -> None:
        a = DatabaseIngestLock()
        assert _acquire(a)
        asyncio.run(a.release())
        asyncio.run(a.release())
        assert db.rows == {}

    def test_every_acquisition_has_its_own_holder_id(self, db: FakeDatabase) -> None:
        a = DatabaseIngestLock()
        assert _acquire(a)
        first = a.holder
        asyncio.run(a.release())
        assert _acquire(a) and a.holder != first

    def test_a_renewal_without_a_lock_is_not_a_renewal(self, db: FakeDatabase) -> None:
        assert asyncio.run(DatabaseIngestLock().renew()) is False


class TestWhenTheDatabaseCannotSay:
    def test_acquire_fails_closed_and_logs_only_the_kind(
        self, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = Recorder()
        monkeypatch.setattr(lock_mod, "_log", log)
        db.down = True
        lock = DatabaseIngestLock()
        with pytest.raises(IngestLockUnavailable) as caught:
            _acquire(lock)
        assert caught.value.status_code == 503 and caught.value.code == "ingest_lock_unavailable"
        assert lock.holder is None
        assert any(
            line["message"] == "ingest lock unavailable" and line["kind"] == "OperationalError"
            for line in log.lines
        )
        assert "connection reset" not in str(log.lines)

    def test_a_deadlock_or_dropped_connection_is_tried_once_more(self, db: FakeDatabase) -> None:
        db.errors = [OperationalError("UPDATE", {}, Exception("deadlock victim"))]
        lock = DatabaseIngestLock()
        assert _acquire(lock) is True and lock.holder is not None

    def test_two_failures_in_a_row_are_503(self, db: FakeDatabase) -> None:
        db.errors = [OperationalError("UPDATE", {}, Exception("x")) for _ in range(2)]
        with pytest.raises(IngestLockUnavailable):
            _acquire(DatabaseIngestLock())

    def test_a_call_that_never_answers_is_503_after_its_limit(
        self, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lock_mod, "ACQUIRE_TIMEOUT_SECONDS", 0.05)
        db.hang = {"take_over"}
        lock = DatabaseIngestLock()
        with pytest.raises(IngestLockUnavailable):
            _acquire(lock)
        assert lock.holder is None

    def test_an_integrity_error_with_no_row_behind_it_is_not_taken_for_held(
        self, db: FakeDatabase
    ) -> None:
        db.phantom_integrity_error = True
        lock = DatabaseIngestLock()
        with pytest.raises(IngestLockUnavailable):
            _acquire(lock)
        assert lock.holder is None

    def test_any_other_error_is_503_too_not_a_500(self, db: FakeDatabase) -> None:
        db.errors = [RuntimeError("anything at all")]
        with pytest.raises(IngestLockUnavailable):
            _acquire(DatabaseIngestLock())

    def test_a_cancel_while_the_commit_waits_leaves_the_holder_to_release(
        self, db: FakeDatabase
    ) -> None:
        async def go() -> None:
            db.commit_gate = asyncio.Event()  # the commit never finishes
            lock = DatabaseIngestLock()
            task = asyncio.create_task(lock.acquire())
            await _until(db.commit_started.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert lock.holder is not None and "ingest" in db.rows
            db.commit_gate = None
            await lock.release()
            assert db.rows == {} and lock.holder is None

        asyncio.run(go())

    def test_release_never_raises(self, db: FakeDatabase) -> None:
        lock = DatabaseIngestLock()
        assert _acquire(lock)
        db.down = True
        asyncio.run(lock.release())  # the row expires by itself
        assert lock.holder is None

    def test_release_does_not_wait_for_a_database_that_never_answers(
        self, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lock_mod, "RELEASE_TIMEOUT_SECONDS", 0.05)
        lock = DatabaseIngestLock()
        assert _acquire(lock)
        db.hang = {"release"}
        started = time.monotonic()
        asyncio.run(lock.release())
        assert time.monotonic() - started < 2.0 and lock.holder is None

    def test_a_renewal_error_is_raised_for_the_caller_to_judge(self, db: FakeDatabase) -> None:
        lock = DatabaseIngestLock()
        assert _acquire(lock)
        db.down = True
        with pytest.raises(OperationalError):
            asyncio.run(lock.renew())

    def test_a_ttl_too_short_to_renew_inside_is_refused(self) -> None:
        with pytest.raises(ValueError):
            DatabaseIngestLock(ttl_seconds=2)

    def test_the_defaults(self) -> None:
        lock = DatabaseIngestLock()
        assert lock.ttl_seconds == DEFAULT_TTL_SECONDS == 300
        assert lock.heartbeat_seconds == 100
        assert lock.renew_timeout_seconds == 20
        assert DatabaseIngestLock(ttl_seconds=30).renew_timeout_seconds == 10


class TestWhenItIsUsed:
    @pytest.mark.parametrize(
        ("url", "configured"),
        [
            (None, False),
            ("", False),
            (DEFAULT_DATABASE_URL, False),
            (REAL_URL, True),
            ("not a url at all", True),  # unparsable: configured but broken, so it fails closed
        ],
    )
    def test_only_a_configured_database_counts(
        self, monkeypatch: pytest.MonkeyPatch, url: str | None, configured: bool
    ) -> None:
        if url is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", url)
        assert database_is_configured() is configured

    def test_a_configured_database_gets_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", REAL_URL)
        assert isinstance(ingest_job.new_lock(), DatabaseIngestLock)

    def test_a_local_run_with_no_database_has_no_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        monkeypatch.setenv("APP_ENV", "local")
        assert ingest_job.new_lock() is None

    @pytest.mark.parametrize("env", ["dev", "staging", "prod"])
    def test_a_deployed_environment_without_a_database_url_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, env: str
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("APP_ENV", env)
        with pytest.raises(IngestLockUnavailable):
            ingest_job.new_lock()


# ── the job ──────────────────────────────────────────────────────────────────


class FakeLock:
    """What ``ingest_job`` needs of a lock, recording what it did."""

    def __init__(self, *, acquire: bool = True, heartbeat: float = 0.01, ttl: float = 300) -> None:
        self.acquire_result = acquire
        self.heartbeat_seconds = heartbeat
        self.ttl_seconds = ttl
        self.renew_timeout_seconds: float = min(20.0, heartbeat)
        self.calls: list[str] = []
        self.renewals: list[Any] = []  # what each renewal answers, then True: bool|Exception|HANG

    async def acquire(self) -> bool:
        self.calls.append("acquire")
        return self.acquire_result

    async def renew(self) -> bool:
        self.calls.append("renew")
        answer = self.renewals.pop(0) if self.renewals else True
        if isinstance(answer, str) and answer == HANG:
            await asyncio.sleep(3600)
        if isinstance(answer, Exception):
            raise answer
        return bool(answer)

    async def release(self) -> None:
        self.calls.append("release")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("INGEST_ADMIN_KEY", raising=False)
    monkeypatch.delenv("INGEST_MAX_RUN_SECONDS", raising=False)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("AZURE_AI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_AI_EMBEDDING_DEPLOYMENT", "embed")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.invalid")
    monkeypatch.setenv("AZURE_SEARCH_INDEX", "docs")
    ingest_job._reset_for_tests()
    yield
    ingest_job._reset_for_tests()


def _runs(monkeypatch: pytest.MonkeyPatch, work: Any) -> None:
    monkeypatch.setattr(ingest_job, "ingest_corpus", work)


async def _ok(**kwargs: Any) -> IngestReport:
    return IngestReport(documents=1, chunks=1, uploaded=1, seconds=0.01)


async def _forever(**kwargs: Any) -> IngestReport:
    await asyncio.sleep(3600)
    return IngestReport()


def _spy_kinds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The error kinds of the failures the job reports."""
    kinds: list[str] = []
    real = ingest_job._report_failure

    def spy(exc: BaseException, seconds: float) -> None:
        kinds.append(ingest_job.error_kind(exc))
        real(exc, seconds)

    monkeypatch.setattr(ingest_job, "_report_failure", spy)
    return kinds


async def _start_and_finish() -> None:
    await ingest_job.start()
    task = ingest_job._task
    assert task is not None
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestJobWithTheLock:
    def test_it_takes_the_lock_before_the_run_and_gives_it_back_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = FakeLock()
        _runs(monkeypatch, _ok)
        monkeypatch.setattr(ingest_job, "new_lock", lambda: lock)
        asyncio.run(_start_and_finish())
        assert lock.calls[0] == "acquire" and lock.calls[-1] == "release"
        assert lock.calls.count("release") == 1

    def test_another_replica_holding_it_is_409_and_nothing_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = FakeLock(acquire=False)
        _runs(monkeypatch, _ok)
        monkeypatch.setattr(ingest_job, "new_lock", lambda: lock)

        async def go() -> None:
            with pytest.raises(Conflict) as caught:
                await ingest_job.start()
            assert caught.value.code == "ingest_running"
            assert not ingest_job.is_running()

        asyncio.run(go())
        assert lock.calls == ["acquire"]  # no release of a lock it never held

    def test_a_run_in_this_process_is_refused_before_the_database_is_asked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[str] = []

        class Pending:
            def done(self) -> bool:
                return False

        ingest_job._task = Pending()  # type: ignore[assignment]
        monkeypatch.setattr(ingest_job, "new_lock", lambda: asked.append("new_lock"))

        async def go() -> None:
            with pytest.raises(Conflict):
                await ingest_job.start()

        asyncio.run(go())
        assert asked == []

    def test_it_is_given_back_when_the_run_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lock = FakeLock()

        async def fails(**kwargs: Any) -> IngestReport:
            raise RuntimeError("boom")

        _runs(monkeypatch, fails)
        monkeypatch.setattr(ingest_job, "new_lock", lambda: lock)
        kinds = _spy_kinds(monkeypatch)
        asyncio.run(_start_and_finish())
        assert lock.calls[-1] == "release" and kinds == ["unexpected"]

    def test_it_is_given_back_when_the_run_is_cancelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = FakeLock()
        _runs(monkeypatch, _forever)
        monkeypatch.setattr(ingest_job, "new_lock", lambda: lock)

        async def go() -> None:
            await ingest_job.start()
            await _until(lambda: "renew" in lock.calls)  # the run and its heartbeat are going
            task = ingest_job._task
            assert task is not None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(go())
        assert lock.calls[-1] == "release" and lock.calls.count("release") == 1

    def test_a_database_that_cannot_say_stops_the_start_with_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Unavailable(FakeLock):
            async def acquire(self) -> bool:
                raise IngestLockUnavailable()

        monkeypatch.setattr(ingest_job, "new_lock", lambda: Unavailable())
        _runs(monkeypatch, _ok)

        async def go() -> None:
            with pytest.raises(IngestLockUnavailable):
                await ingest_job.start()
            assert not ingest_job.is_running()

        asyncio.run(go())

    def test_a_cancel_while_acquiring_gives_the_lock_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Slow(FakeLock):
            async def acquire(self) -> bool:
                self.calls.append("acquire")
                await asyncio.sleep(3600)
                return True

        lock = Slow()
        monkeypatch.setattr(ingest_job, "new_lock", lambda: lock)

        async def go() -> None:
            starting = asyncio.create_task(ingest_job.start())
            await _until(lambda: "acquire" in lock.calls)
            starting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await starting

        asyncio.run(go())
        assert lock.calls == ["acquire", "release"] and not ingest_job.is_running()

    def test_the_lock_is_given_back_if_the_run_cannot_be_scheduled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = FakeLock()
        monkeypatch.setattr(ingest_job, "new_lock", lambda: lock)

        def boom(lock: Any) -> None:
            raise RuntimeError("the run cannot be built")

        monkeypatch.setattr(ingest_job, "_execute", boom)

        async def go() -> None:
            with pytest.raises(RuntimeError):
                await ingest_job.start()
            assert not ingest_job.is_running()

        asyncio.run(go())
        assert lock.calls == ["acquire", "release"]

    def test_with_no_database_there_is_no_lock_and_no_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ingest_job, "new_lock", lambda: None)
        started: list[str] = []
        real = job_mod._keep_alive

        async def spy(*args: Any) -> None:
            started.append("heartbeat")
            await real(*args)

        monkeypatch.setattr(job_mod, "_keep_alive", spy)
        _runs(monkeypatch, _ok)
        asyncio.run(_start_and_finish())
        assert started == []

    def test_two_requests_at_once_in_one_process_start_exactly_one_run(
        self, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", REAL_URL)
        runs: list[int] = []

        async def go() -> list[Any]:
            release = asyncio.Event()

            async def long(**kwargs: Any) -> IngestReport:
                runs.append(1)
                await release.wait()
                return IngestReport(documents=1)

            _runs(monkeypatch, long)
            results = await asyncio.gather(
                ingest_job.start(), ingest_job.start(), return_exceptions=True
            )
            await _until(lambda: len(runs) == 1)
            release.set()
            task = ingest_job._task
            assert task is not None
            await task
            return list(results)

        results = asyncio.run(go())
        assert sum(r is None for r in results) == 1
        assert sum(isinstance(r, Conflict) for r in results) == 1
        assert runs == [1] and db.rows == {}  # one run, and it gave the lock back


class TestTheHeartbeat:
    def _run(self, monkeypatch: pytest.MonkeyPatch, lock: FakeLock, work: Any) -> list[str]:
        _runs(monkeypatch, work)
        monkeypatch.setattr(ingest_job, "new_lock", lambda: lock)
        kinds = _spy_kinds(monkeypatch)
        asyncio.run(_start_and_finish())
        return kinds

    def test_a_long_run_renews_the_lock_while_it_goes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = FakeLock(heartbeat=0.005)

        async def until_renewed(**kwargs: Any) -> IngestReport:
            await _until(lambda: lock.calls.count("renew") >= 5)
            return IngestReport(documents=1)

        assert self._run(monkeypatch, lock, until_renewed) == []
        assert lock.calls.count("renew") >= 5 and lock.calls[-1] == "release"

    def test_the_heartbeat_ends_with_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lock = FakeLock(heartbeat=0.005)
        _runs(monkeypatch, _ok)
        monkeypatch.setattr(ingest_job, "new_lock", lambda: lock)

        async def go() -> tuple[int, int]:
            await _start_and_finish()
            before = lock.calls.count("renew")
            await asyncio.sleep(0.1)  # twenty heartbeats' worth
            return before, lock.calls.count("renew")

        before, after = asyncio.run(go())
        assert before == after

    def test_a_lock_that_is_lost_stops_the_run_and_is_reported_as_lock_lost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = FakeLock(heartbeat=0.005)
        lock.renewals = [True, False]  # the second renewal finds it taken over
        stopped: list[str] = []

        async def long(**kwargs: Any) -> IngestReport:
            try:
                await asyncio.sleep(3600)
            finally:
                stopped.append("stopped")
            return IngestReport()

        assert self._run(monkeypatch, lock, long) == ["lock_lost"]
        assert stopped == ["stopped"] and lock.calls[-1] == "release"

    def test_a_renewal_that_fails_once_is_forgiven(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lock = FakeLock(heartbeat=0.005, ttl=300)
        lock.renewals = [OperationalError("UPDATE", {}, Exception("reset")), True]

        async def until_renewed_again(**kwargs: Any) -> IngestReport:
            await _until(lambda: lock.calls.count("renew") >= 3)
            return IngestReport(documents=1)

        assert self._run(monkeypatch, lock, until_renewed_again) == []

    def test_renewals_that_keep_failing_lose_the_lock_before_it_expires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ttl 0.06, heartbeat 0.02: the run is stopped after ttl - heartbeat = 0.04 s without a
        # successful renewal, i.e. before the row would expire at 0.06
        lock = FakeLock(heartbeat=0.02, ttl=0.06)
        lock.renewals = [OperationalError("UPDATE", {}, Exception("reset")) for _ in range(1000)]
        assert self._run(monkeypatch, lock, _forever) == ["lock_lost"]

    def test_the_give_up_time_is_before_the_expiry_by_one_heartbeat(self) -> None:
        lock = DatabaseIngestLock(ttl_seconds=300)
        assert lock.ttl_seconds - lock.heartbeat_seconds == 200 < lock.ttl_seconds

    def test_a_renewal_that_never_answers_counts_as_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = FakeLock(heartbeat=0.02, ttl=0.1)
        lock.renew_timeout_seconds = 0.02
        lock.renewals = [HANG] * 1000  # a black-holed connection: no error, no answer
        started = time.monotonic()
        assert self._run(monkeypatch, lock, _forever) == ["lock_lost"]
        assert time.monotonic() - started < 10

    def test_a_heartbeat_that_dies_stops_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lock = FakeLock(heartbeat=0.005)
        lock.heartbeat_seconds = "not a number"  # type: ignore[assignment]  # the task dies at once
        assert self._run(monkeypatch, lock, _forever) == ["lock_lost"]

    def test_a_failed_renewal_logs_its_kind_and_never_its_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = Recorder()
        monkeypatch.setattr(job_mod, "_log", log)
        lock = FakeLock(heartbeat=0.005)
        lock.renewals = [OperationalError("UPDATE", {}, Exception("SECRET driver text")), True]

        async def until_renewed_again(**kwargs: Any) -> IngestReport:
            await _until(lambda: lock.calls.count("renew") >= 3)
            return IngestReport(documents=1)

        self._run(monkeypatch, lock, until_renewed_again)
        assert any(
            line["message"] == "ingest lock renewal failed" and line["kind"] == "OperationalError"
            for line in log.lines
        )
        assert "SECRET" not in str(log.lines)


class TestTheDeadline:
    @pytest.mark.parametrize("with_lock", [True, False])
    def test_a_run_that_outlives_its_deadline_stops_as_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch, with_lock: bool
    ) -> None:
        monkeypatch.setenv("INGEST_MAX_RUN_SECONDS", "0.05")
        lock = FakeLock(heartbeat=0.005) if with_lock else None
        monkeypatch.setattr(ingest_job, "new_lock", lambda: lock)
        kinds = _spy_kinds(monkeypatch)
        _runs(monkeypatch, _forever)  # keeps "running" (and its lock renewed) for ever
        asyncio.run(_start_and_finish())
        assert kinds == ["timeout"]
        if lock is not None:
            assert lock.calls[-1] == "release"

    def test_the_default_deadline_and_its_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.ai.config import DEFAULT_INGEST_MAX_RUN_SECONDS, get_ai_settings

        assert get_ai_settings().ingest_max_run_seconds == DEFAULT_INGEST_MAX_RUN_SECONDS == 1800
        monkeypatch.setenv("INGEST_MAX_RUN_SECONDS", "60")
        assert get_ai_settings().ingest_max_run_seconds == 60


def test_lock_lost_and_timeout_are_known_bounded_kinds() -> None:
    assert ingest_job.error_kind(ingest_job.IngestLockLost()) == "lock_lost"
    assert ingest_job.error_kind(TimeoutError()) == "timeout"


# ── the endpoint ─────────────────────────────────────────────────────────────


class TestEndpoint:
    @pytest.fixture
    def key(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
        monkeypatch.setenv("INGEST_ADMIN_KEY", KEY)
        return {"x-admin-key": KEY}

    def test_a_run_started_in_another_replica_is_409_here_too(
        self, monkeypatch: pytest.MonkeyPatch, key: dict[str, str], db: FakeDatabase
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", REAL_URL)
        gate = threading.Event()
        started: list[int] = []

        async def long(**kwargs: Any) -> IngestReport:
            started.append(1)
            while not gate.is_set():
                await asyncio.sleep(0.005)
            return IngestReport(documents=1)

        _runs(monkeypatch, long)
        with TestClient(app) as client:
            assert client.post("/v1/admin/ingest", headers=key).status_code == 202
            _wait(lambda: started == [1])
            # "the other replica": this process has no run of its own to see, only the row
            ingest_job._task = None
            second = client.post("/v1/admin/ingest", headers=key)
            assert second.status_code == 409 and second.json()["error"] == "ingest_running"
            assert started == [1]  # the refused call started nothing
            gate.set()
            _wait(lambda: db.rows == {})  # the first run gave the lock back

    def test_a_database_that_cannot_be_reached_is_503_ingest_lock_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, key: dict[str, str], db: FakeDatabase
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", REAL_URL)
        db.down = True
        _runs(monkeypatch, _ok)
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers=key)
        assert res.status_code == 503 and res.json()["error"] == "ingest_lock_unavailable"
        assert not ingest_job.is_running()

    def test_a_deployed_api_with_no_database_url_is_503_not_an_unlocked_run(
        self, monkeypatch: pytest.MonkeyPatch, key: dict[str, str]
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("APP_ENV", "prod")
        _runs(monkeypatch, _ok)
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers=key)
        assert res.status_code == 503 and res.json()["error"] == "ingest_lock_unavailable"

    def test_the_guard_still_comes_first(
        self, monkeypatch: pytest.MonkeyPatch, db: FakeDatabase
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", REAL_URL)
        monkeypatch.setenv("INGEST_ADMIN_KEY", KEY)
        db.down = True  # a caller without the key must not learn the database is down
        with TestClient(app) as client:
            res = client.post("/v1/admin/ingest", headers={"x-admin-key": "wrong"})
        assert res.status_code == 401

    def test_without_a_configured_database_nothing_touches_one(
        self, monkeypatch: pytest.MonkeyPatch, key: dict[str, str], db: FakeDatabase
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        db.down = True  # would raise if anything asked it
        _runs(monkeypatch, _ok)
        with TestClient(app) as client:
            assert client.post("/v1/admin/ingest", headers=key).status_code == 202
            _wait(lambda: not ingest_job.is_running())
        assert db.commits == 0 and db.rows == {}
