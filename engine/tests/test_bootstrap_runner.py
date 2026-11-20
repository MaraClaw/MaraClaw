from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import psycopg
import pytest

from app.db.errors import DbError, LockNotAvailableError, QueryCanceledError, UniqueViolationError
from app.scripts import bootstrap_db


async def test_missing_baseline_is_fatal_and_closes_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Given an absent baseline and an isolated pool boundary.
    close = AsyncMock()
    monkeypatch.setattr(bootstrap_db, "SCHEMA_BASELINE", tmp_path / "missing.sql")
    monkeypatch.setattr(bootstrap_db, "init_pool", AsyncMock())
    monkeypatch.setattr(bootstrap_db, "close_pool", close)
    # When initialization runs, Then the missing file propagates and cleanup runs.
    with pytest.raises(FileNotFoundError):
        await bootstrap_db.main()
    close.assert_awaited_once()


@pytest.mark.parametrize("error", [DbError("invalid schema"), UniqueViolationError("duplicate membership")])
async def test_schema_errors_are_fatal_and_close_pool(monkeypatch: pytest.MonkeyPatch, error: DbError) -> None:
    # Given a failing SQL boundary (including duplicate memberships).
    close = AsyncMock()
    monkeypatch.setattr(bootstrap_db, "init_pool", AsyncMock())
    monkeypatch.setattr(bootstrap_db, "close_pool", close)
    monkeypatch.setattr(bootstrap_db, "_apply_sql_script", AsyncMock(side_effect=error))
    # When initialization runs, Then it fails rather than accepting an upgrade.
    with pytest.raises(type(error)):
        await bootstrap_db.main()
    close.assert_awaited_once()


async def test_pool_open_failure_still_closes_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a pool-open failure.
    close = AsyncMock()
    monkeypatch.setattr(bootstrap_db, "init_pool", AsyncMock(side_effect=OSError("pool open failed")))
    monkeypatch.setattr(bootstrap_db, "close_pool", close)
    # When initialization runs, Then its failure propagates through cleanup.
    with pytest.raises(OSError):
        await bootstrap_db.main()
    close.assert_awaited_once()


class _Connection:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self._log = log

    async def execute(self, query: str, params: object = None) -> None:
        self._log.append((query, params))


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Everything the runner does on its connection, in order."""
    log: list[tuple[str, object]] = []

    @asynccontextmanager
    async def connection_ctx() -> AsyncGenerator[_Connection]:
        yield _Connection(log)

    @asynccontextmanager
    async def long_running_statement(_conn: _Connection) -> AsyncGenerator[None]:
        log.append(("lift statement_timeout", None))
        yield

    monkeypatch.setattr(bootstrap_db, "connection_ctx", connection_ctx)
    monkeypatch.setattr(bootstrap_db, "long_running_statement", long_running_statement)
    return log


@pytest.fixture
def small_baseline(tmp_path: Path) -> Path:
    path = tmp_path / "baseline.sql"
    _ = path.write_text("CREATE TABLE a (id int);\nCREATE TABLE b (id int);\n", encoding="utf-8")
    return path


async def test_run_holds_the_advisory_lock_and_then_bounds_lock_waits_before_any_baseline_statement(
    executed: list[tuple[str, object]], small_baseline: Path
) -> None:
    # Given a baseline of two statements.
    # When the runner applies it.
    await bootstrap_db._apply_sql_script(small_baseline)
    # Then the statement limit is lifted so queueing behind a peer is not cut off, the transaction-level advisory
    # lock is taken next, the transaction-local lock bound follows it, and only then does any DDL run.
    lifted, advisory, bound, *ddl = executed
    assert lifted[0] == "lift statement_timeout"
    assert "pg_advisory_xact_lock" in advisory[0]
    assert advisory[1] == {"key": bootstrap_db._ADVISORY_LOCK_KEY}
    assert "'lock_timeout'" in bound[0]
    assert bound[0].endswith(", true)")
    assert bound[1] == {"milliseconds": str(bootstrap_db._LOCK_TIMEOUT_MS)}
    assert [query for query, _ in ddl] == ["CREATE TABLE a (id int)", "CREATE TABLE b (id int)"]


@pytest.fixture
def bounded_retries(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(bootstrap_db, "_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(bootstrap_db, "_RETRY_DELAY_SECONDS", 0.0)
    return 3


_RETRYABLE: list[Callable[[], BaseException]] = [
    lambda: LockNotAvailableError(orig=psycopg.errors.LockNotAvailable()),
    lambda: DbError("deadlock detected", orig=psycopg.errors.DeadlockDetected()),
    psycopg.errors.DeadlockDetected,
]
_FATAL: list[Callable[[], BaseException]] = [
    lambda: UniqueViolationError("duplicate membership", orig=psycopg.errors.UniqueViolation()),
    lambda: DbError("syntax error", orig=psycopg.errors.SyntaxError()),
    lambda: QueryCanceledError(orig=psycopg.errors.QueryCanceled()),
    lambda: DbError("no sqlstate"),
    psycopg.errors.InsufficientPrivilege,
]


@pytest.mark.parametrize("error", _RETRYABLE, ids=["lock-not-available", "deadlock", "raw-deadlock"])
async def test_a_lock_timeout_or_deadlock_retries_the_whole_transaction_until_one_attempt_succeeds(
    monkeypatch: pytest.MonkeyPatch, small_baseline: Path, bounded_retries: int, error: Callable[[], BaseException]
) -> None:
    # Given the first attempt fails and the second succeeds.
    attempt = AsyncMock(side_effect=[error(), None])
    monkeypatch.setattr(bootstrap_db, "_apply_once", attempt)
    # When the runner applies the baseline.
    await bootstrap_db._apply_sql_script(small_baseline)
    # Then it retried once, handing the same statements to a fresh transaction.
    assert attempt.await_count == 2
    assert attempt.await_args_list[0] == attempt.await_args_list[1]


@pytest.mark.parametrize("error", _RETRYABLE, ids=["lock-not-available", "deadlock", "raw-deadlock"])
async def test_retries_are_bounded_then_the_run_fails_and_closes_the_pool(
    monkeypatch: pytest.MonkeyPatch, bounded_retries: int, error: Callable[[], BaseException]
) -> None:
    # Given every attempt hits the same lock failure.
    close = AsyncMock()
    attempt = AsyncMock(side_effect=[error() for _ in range(bounded_retries)])
    monkeypatch.setattr(bootstrap_db, "init_pool", AsyncMock())
    monkeypatch.setattr(bootstrap_db, "close_pool", close)
    monkeypatch.setattr(bootstrap_db, "_apply_once", attempt)
    # When the runner runs, Then it gives up after the bound with the failure, so the process exits non-zero.
    with pytest.raises(type(error())):
        await bootstrap_db.main()
    assert attempt.await_count == bounded_retries
    close.assert_awaited_once()


@pytest.mark.parametrize("error", _FATAL, ids=["unique", "syntax", "query-canceled", "plain", "raw-privilege"])
async def test_every_other_sql_error_is_fatal_on_the_first_attempt(
    monkeypatch: pytest.MonkeyPatch, small_baseline: Path, bounded_retries: int, error: Callable[[], BaseException]
) -> None:
    # Given an error that is neither a lock timeout nor a deadlock.
    attempt = AsyncMock(side_effect=error())
    monkeypatch.setattr(bootstrap_db, "_apply_once", attempt)
    # When the runner applies the baseline, Then it is not retried.
    with pytest.raises(type(error())):
        await bootstrap_db._apply_sql_script(small_baseline)
    assert attempt.await_count == 1
