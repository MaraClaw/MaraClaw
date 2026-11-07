"""Tests for context-bound psycopg session helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextvars import Context
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow
from tests.test_redis_cache import FakeRedis

from app.core import access_cache, redis_cache
from app.db import pool as pool_module, session as session_module
from app.db.connection import DbConnection
from app.db.session import (
    bind_crud_connection,
    connection_ctx,
    flush_request_transaction,
    get_connection,
    long_running_statement,
    transaction,
)
from app.records.user import UserRecord
from app.services import agent_setup_lock, openclaw_update_exclusion as exclusion
from app.services.agent_setup_lock import WorkspaceLockTimeoutError, setup_workspace_lock
from transaction_pool_fakes import CountingPool

_REAL_FLUSH_VERSIONS = redis_cache.flush_deferred_versions


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent
        self._last = ""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        del exc_type, exc, tb
        return False

    async def execute(self, query: object, params: object = None) -> None:
        self._last = str(query)
        self._parent.executed.append((self._last, params))
        if "set_config('statement_timeout'" in self._last:
            value = cast("dict[str, str]", params)["value"]
            if value in self._parent.failing_values:
                raise RuntimeError("restore failed")
            self._parent.timeout = value
            self._parent.timeouts.append(value)

    async def fetchone(self) -> dict[str, str] | None:
        if "current_setting('statement_timeout')" in self._last:
            return {"value": self._parent.timeout}
        return None

    async def fetchall(self) -> list[Any]:
        return []

    async def executemany(self, query: object, params_seq: object = None) -> None:
        del query, params_seq


class _FakeTransaction:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        del exc_type, exc, tb
        return False


class _FakeRawConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.timeout = "30s"
        self.timeouts: list[str] = []
        self.failing_values: set[str] = set()
        self.info = SimpleNamespace(transaction_status=TransactionStatus.INTRANS)

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


def _wrap(raw: _FakeRawConnection) -> DbConnection:
    return DbConnection(cast("AsyncConnection[DictRow]", raw))


class _PoolConnectionCM:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self._raw = raw

    async def __aenter__(self) -> _FakeRawConnection:
        return self._raw

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _FakePool:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self._raw = raw

    def connection(self) -> _PoolConnectionCM:
        return _PoolConnectionCM(self._raw)


@pytest.fixture(autouse=True)
def _clear_conn_ctx() -> Iterator[None]:
    token = session_module._conn_ctx.set(None)
    yield
    session_module._conn_ctx.reset(token)
    pool_module.reset_pool_for_tests()


def _install_cache(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis_cache.reset_circuit()
    cache = FakeRedis()

    async def client() -> FakeRedis:
        return cache

    monkeypatch.setattr(redis_cache, "_client", client)
    return cache


@pytest.mark.asyncio
async def test_connection_ctx_commits_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    # When
    async with connection_ctx() as db:
        assert isinstance(db, DbConnection)
        assert get_connection() is db
        await db.execute("SELECT 1")

    # Then
    assert raw.commits == 1
    assert raw.rollbacks == 0
    assert get_connection() is None


@pytest.mark.asyncio
async def test_connection_ctx_rollbacks_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async def _failing_scope() -> None:
        async with connection_ctx() as db:
            await db.execute("SELECT 1")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await _failing_scope()

    assert raw.commits == 0
    assert raw.rollbacks == 1
    assert get_connection() is None


@pytest.mark.asyncio
async def test_nested_transaction_joins_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async with connection_ctx() as outer, transaction() as inner:
        assert inner is outer
        await inner.execute("SELECT 2")

    # Only the outer scope commits once.
    assert raw.commits == 1
    assert raw.rollbacks == 0


@pytest.mark.asyncio
async def test_transaction_with_explicit_connection_commits() -> None:
    raw = _FakeRawConnection()
    db = _wrap(raw)

    async with transaction(db) as bound:
        assert bound is db
        assert get_connection() is db
        await bound.execute("UPDATE t SET x = 1")

    assert raw.commits == 1
    assert get_connection() is None


@pytest.mark.asyncio
async def test_flush_request_transaction_commits_before_request_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    agen = bind_crud_connection()
    await agen.__anext__()
    assert get_connection() is not None
    await flush_request_transaction()
    assert raw.commits == 1
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()
    assert raw.commits == 2
    assert get_connection() is None


@pytest.mark.asyncio
async def test_snapshot_deletes_publish_after_explicit_request_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    raw = _FakeRawConnection()
    cache = _install_cache(monkeypatch)
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async with connection_ctx():
        await redis_cache.cache_delete("snapshot", defer_until_commit=True)
        await redis_cache.cache_delete("snapshot", defer_until_commit=True)
        assert cache.pipelines == []
        # When
        await flush_request_transaction()
        # Then
        assert (raw.commits, cache.pipelines) == (1, [[("delete", ("snapshot",), None)]])
    assert cache.pipelines == [[("delete", ("snapshot",), None)]]


@pytest.mark.asyncio
async def test_snapshot_deletes_are_discarded_after_request_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    raw = _FakeRawConnection()
    cache = _install_cache(monkeypatch)
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async def failing_scope() -> None:
        async with connection_ctx():
            await redis_cache.cache_delete("snapshot", defer_until_commit=True)
            raise RuntimeError("boom")

    # When
    with pytest.raises(RuntimeError, match="boom"):
        await failing_scope()
    async with connection_ctx():
        pass
    # Then
    assert cache.pipelines == []
    assert raw.rollbacks == 1


@pytest.mark.asyncio
async def test_bind_crud_connection_is_noop_without_pool() -> None:
    assert get_connection() is None
    agen = bind_crud_connection()
    await agen.__anext__()
    assert get_connection() is None
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()


@pytest.mark.asyncio
async def test_bind_crud_connection_shares_one_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    agen = bind_crud_connection()
    await agen.__anext__()
    outer = get_connection()
    assert outer is not None
    async with connection_ctx() as inner:
        assert inner is outer
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()
    assert raw.commits == 1
    assert get_connection() is None


@pytest.fixture
def callback_scope(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeRawConnection, list[str]]:
    raw = _FakeRawConnection()
    events: list[str] = []
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async def commit() -> None:
        raw.commits += 1
        events.append("commit")

    async def flush_acl() -> None:
        events.append("acl")

    async def flush_versions() -> None:
        events.append("versions")

    monkeypatch.setattr(raw, "commit", commit)
    monkeypatch.setattr(access_cache, "flush_deferred_acl", flush_acl)
    monkeypatch.setattr(redis_cache, "flush_deferred_versions", flush_versions)
    return raw, events


async def _queue_cache_writes() -> None:
    await access_cache.bump_agent_acl_version(uuid4())
    await redis_cache.bump_version("ver")


def test_after_commit_without_scope_runs_immediately() -> None:
    # Given
    events: list[str] = []
    # When
    session_module.run_after_commit(lambda: events.append("commit"), lambda: events.append("rollback"))
    # Then
    assert events == ["commit"]


@pytest.mark.asyncio
async def test_after_commit_runs_in_order_after_deferred_flushes(callback_scope) -> None:
    # Given
    _, events = callback_scope
    # When
    async with connection_ctx():
        await _queue_cache_writes()
        session_module.run_after_commit(lambda: events.append("first"))
        session_module.run_after_commit(lambda: events.append("second"))
        assert events == []
    # Then
    assert events == ["commit", "acl", "versions", "first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("body failed"), asyncio.CancelledError("cancelled")])
async def test_after_commit_body_failure_only_runs_rollback(callback_scope, failure: BaseException) -> None:
    # Given
    raw, events = callback_scope

    async def scope() -> None:
        async with connection_ctx():
            session_module.run_after_commit(lambda: events.append("launched"), lambda: events.append("dropped"))
            raise failure

    # When
    with pytest.raises(type(failure)):
        await scope()
    # Then
    assert events == ["dropped"]
    assert raw.commits == 0


@pytest.mark.asyncio
async def test_after_commit_commit_failure_only_runs_rollback(callback_scope, monkeypatch) -> None:
    # Given
    raw, events = callback_scope

    async def fail_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(raw, "commit", fail_commit)
    # When
    with pytest.raises(RuntimeError, match="commit failed"):
        async with connection_ctx():
            session_module.run_after_commit(lambda: events.append("launched"), lambda: events.append("dropped"))
    # Then
    assert events == ["dropped"]
    assert raw.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("flush failed"), asyncio.CancelledError("flush cancelled")])
async def test_after_commit_flush_failure_still_launches(callback_scope, monkeypatch, failure: BaseException) -> None:
    # Given
    _, events = callback_scope

    async def fail_flush() -> None:
        raise failure

    monkeypatch.setattr(access_cache, "flush_deferred_acl", fail_flush)

    async def scope() -> None:
        async with connection_ctx():
            await _queue_cache_writes()
            session_module.run_after_commit(lambda: events.append("launched"), lambda: events.append("dropped"))

    # When
    with pytest.raises(type(failure)):
        await scope()
    # Then the failure surfaces, yet the sibling family was still attempted and the callbacks launched
    assert events == ["commit", "versions", "launched"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
async def test_after_commit_callback_failure_is_logged_and_next_runs(callback_scope, monkeypatch, rollback) -> None:
    # Given
    _, events = callback_scope
    logger = Mock()
    monkeypatch.setattr(session_module, "logger", logger, raising=False)
    failure = ValueError("callback failed")

    def fail_callback() -> None:
        raise failure

    # When
    async def scope() -> None:
        async with connection_ctx():
            session_module.run_after_commit(fail_callback, fail_callback)
            session_module.run_after_commit(lambda: events.append("next"), lambda: events.append("next"))
            if rollback:
                raise RuntimeError("body failed")

    if rollback:
        with pytest.raises(RuntimeError, match="body failed"):
            await scope()
    else:
        await scope()
    # Then
    assert events[-1] == "next"
    logger.opt.assert_called_once_with(exception=failure)
    logger.opt.return_value.error.assert_called_once()


@pytest.mark.asyncio
async def test_commit_without_pending_invalidations_schedules_no_flush(callback_scope) -> None:
    # Given a scope that queued nothing
    _, events = callback_scope
    # When
    async with connection_ctx():
        session_module.run_after_commit(lambda: events.append("launched"))
    # Then neither family was started
    assert events == ["commit", "launched"]


@pytest.mark.asyncio
async def test_after_commit_nested_joins_share_outer_batch(callback_scope) -> None:
    # Given
    _, events = callback_scope
    # When
    async with connection_ctx():
        await _queue_cache_writes()
        async with connection_ctx(), transaction():
            session_module.run_after_commit(lambda: events.append("launched"))
        assert events == []
    # Then
    assert events == ["commit", "acl", "versions", "launched"]


@pytest.mark.asyncio
async def test_after_commit_explicit_flush_starts_new_batch(callback_scope) -> None:
    # Given
    _, events = callback_scope

    async def scope() -> None:
        async with connection_ctx():
            await _queue_cache_writes()
            session_module.run_after_commit(lambda: events.append("first"), lambda: events.append("drop first"))
            await flush_request_transaction()
            assert events == ["commit", "acl", "versions", "first"]
            session_module.run_after_commit(lambda: events.append("second"), lambda: events.append("drop second"))
            raise RuntimeError("later failure")

    # When
    with pytest.raises(RuntimeError, match="later failure"):
        await scope()
    # Then
    assert events == ["commit", "acl", "versions", "first", "drop second"]


@pytest.mark.asyncio
async def test_after_commit_failed_explicit_flush_drops_its_batch(callback_scope, monkeypatch) -> None:
    # Given a caller that catches a rejected explicit commit and keeps going
    raw, events = callback_scope
    commit = raw.commit

    async def reject_once() -> None:
        monkeypatch.setattr(raw, "commit", commit)
        events.append("rejected")
        raise RuntimeError("commit rejected")

    monkeypatch.setattr(raw, "commit", reject_once)
    # When
    async with connection_ctx():
        await _queue_cache_writes()
        session_module.run_after_commit(lambda: events.append("launched"), lambda: events.append("dropped"))
        with pytest.raises(RuntimeError, match="commit rejected"):
            await flush_request_transaction()
        session_module.run_after_commit(lambda: events.append("later"), lambda: events.append("drop later"))
    # Then the rolled-back registration is dropped and only later work launches
    assert events == ["rejected", "dropped", "commit", "acl", "versions", "later"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
async def test_after_commit_explicit_transaction_uses_same_lifecycle(callback_scope, rollback) -> None:
    # Given
    raw, events = callback_scope

    async def scope() -> None:
        async with transaction(_wrap(raw)), transaction():
            await _queue_cache_writes()
            session_module.run_after_commit(lambda: events.append("launched"), lambda: events.append("dropped"))
            assert events == []
            if rollback:
                raise RuntimeError("body failed")

    # When
    if rollback:
        with pytest.raises(RuntimeError, match="body failed"):
            await scope()
    else:
        await scope()
    # Then
    assert events == (["dropped"] if rollback else ["commit", "acl", "versions", "launched"])


@pytest.mark.asyncio
async def test_flush_retains_slot_until_connection_owner_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    pool = CountingPool()
    monkeypatch.setattr(session_module, "get_pool", lambda: pool)
    effects: list[tuple[int, int]] = []

    async def borrow() -> None:
        async with connection_ctx():
            effects.append((pool.raw.commits, pool.active))

    async with asyncio.TaskGroup() as tasks:
        async with connection_ctx() as owner:
            session_module.run_after_commit(lambda: effects.append((pool.raw.commits, pool.active)))
            contender = tasks.create_task(borrow(), context=Context())
            async with asyncio.timeout(5):
                await pool.waiting.wait()

            # When
            await flush_request_transaction()

            # Then
            assert effects == [(1, 1)]
            assert (pool.active, pool.checkouts, pool.releases) == (1, 1, 0)
            assert not contender.done()
            assert get_connection() is owner
            async with connection_ctx() as joined, transaction() as nested:
                assert joined is nested is owner
            assert (pool.raw.commits, pool.checkouts, pool.releases) == (1, 1, 0)

        async with asyncio.timeout(5):
            await contender

    assert effects == [(1, 1), (2, 1)]
    assert (pool.active, pool.checkouts, pool.releases) == (0, 2, 2)
    assert (pool.raw.commits, pool.raw.rollbacks) == (3, 0)
    assert get_connection() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("body failed"), asyncio.CancelledError("cancelled")])
async def test_failed_owner_returns_slot_and_discards_commit_effects(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    # Given
    pool = CountingPool()
    monkeypatch.setattr(session_module, "get_pool", lambda: pool)
    effects: list[str] = []

    async def fail_scope() -> None:
        async with connection_ctx():
            session_module.run_after_commit(lambda: effects.append("committed"), lambda: effects.append("dropped"))
            raise failure

    # When
    with pytest.raises(type(failure)):
        await fail_scope()

    # Then
    assert (pool.active, pool.checkouts, pool.releases) == (0, 1, 1)
    assert (pool.raw.commits, pool.raw.rollbacks) == (0, 1)
    assert get_connection() is None
    assert effects == ["dropped"]
    session_module.run_after_commit(lambda: effects.append("outside"))
    async with asyncio.timeout(5), connection_ctx():
        session_module.run_after_commit(lambda: effects.append("next"))
    assert effects == ["dropped", "outside", "next"]
    assert (pool.active, pool.checkouts, pool.releases) == (0, 2, 2)


@pytest.mark.asyncio
async def test_cancelled_task_cleans_owner_context_and_returns_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    pool = CountingPool()
    monkeypatch.setattr(session_module, "get_pool", lambda: pool)
    entered = asyncio.Event()
    hold = asyncio.Event()
    effects: list[str] = []
    restored: list[DbConnection | None] = []

    async def own_connection() -> None:
        try:
            async with connection_ctx():
                session_module.run_after_commit(lambda: effects.append("committed"), lambda: effects.append("dropped"))
                entered.set()
                await hold.wait()
        finally:
            restored.append(get_connection())
            session_module.run_after_commit(lambda: effects.append("outside"))

    async with asyncio.TaskGroup() as tasks:
        owner = tasks.create_task(own_connection())
        async with asyncio.timeout(5):
            await entered.wait()
        # When
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

    # Then
    assert restored == [None]
    assert effects == ["dropped", "outside"]
    assert (pool.active, pool.checkouts, pool.releases) == (0, 1, 1)
    assert (pool.raw.commits, pool.raw.rollbacks) == (0, 1)
    async with asyncio.timeout(5), connection_ctx():
        pass
    assert (pool.active, pool.checkouts, pool.releases) == (0, 2, 2)


@pytest.mark.asyncio
async def test_long_running_statement_lifts_then_restores_actual_prior_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a limit that is not the configured default
    raw = _FakeRawConnection()
    raw.timeout = "1min"
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    # When
    async with connection_ctx() as db:
        async with long_running_statement(db):
            assert raw.timeout == "0"
        assert raw.timeout == "1min"

    # Then the override is transaction-local and the owner still commits once
    assert raw.timeouts == ["0", "1min"]
    assert (raw.commits, raw.rollbacks) == (1, 0)


@pytest.mark.asyncio
async def test_long_running_statement_nested_scopes_unwind_one_level(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async with connection_ctx() as db, long_running_statement(db):
        async with long_running_statement(db):
            assert raw.timeout == "0"
        assert raw.timeout == "0"

    assert raw.timeouts == ["0", "0", "0", "30s"]


@pytest.mark.asyncio
async def test_long_running_statement_failure_restores_and_keeps_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async def failing_scope() -> None:
        async with connection_ctx() as db, long_running_statement(db):
            raise ValueError("body failed")

    with pytest.raises(ValueError, match="body failed"):
        await failing_scope()

    assert raw.timeouts == ["0", "30s"]
    assert (raw.commits, raw.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_long_running_statement_skips_restore_in_aborted_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async def aborted_scope() -> None:
        async with connection_ctx() as db, long_running_statement(db):
            raw.info.transaction_status = TransactionStatus.INERROR
            raise ValueError("statement failed")

    with pytest.raises(ValueError, match="statement failed"):
        await aborted_scope()

    assert raw.timeouts == ["0"]
    assert (raw.commits, raw.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_long_running_statement_restore_failure_does_not_mask_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _FakeRawConnection()
    raw.failing_values = {"30s"}
    logger = Mock()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    monkeypatch.setattr(session_module, "logger", logger)

    async def failing_scope() -> None:
        async with connection_ctx() as db, long_running_statement(db):
            raise ValueError("body failed")

    with pytest.raises(ValueError, match="body failed"):
        await failing_scope()

    assert (raw.commits, raw.rollbacks) == (0, 1)
    logger.opt.assert_called_once()


async def _never() -> None:
    await asyncio.Event().wait()


@pytest.fixture
def short_deadline(monkeypatch: pytest.MonkeyPatch) -> Mock:
    logger = Mock()
    monkeypatch.setattr(session_module, "logger", logger)
    monkeypatch.setattr(session_module, "get_settings", lambda: SimpleNamespace(REDIS_CACHE_WAIT_SECONDS=0.05))
    return logger


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled", ["acl", "versions"])
async def test_stalled_invalidation_family_cannot_suppress_the_other_or_the_callbacks(
    callback_scope, monkeypatch: pytest.MonkeyPatch, short_deadline: Mock, stalled: str
) -> None:
    # Given one family that never answers
    raw, events = callback_scope

    async def healthy() -> None:
        events.append("healthy")

    monkeypatch.setattr(access_cache, "flush_deferred_acl", _never if stalled == "acl" else healthy)
    monkeypatch.setattr(redis_cache, "flush_deferred_versions", _never if stalled == "versions" else healthy)
    # When the scope commits
    async with connection_ctx():
        await _queue_cache_writes()
        session_module.run_after_commit(lambda: events.append("launched"), lambda: events.append("dropped"))
    # Then the other family ran, the commit stood, the stall was abandoned with a warning, callbacks launched
    assert events == ["commit", "healthy", "launched"]
    assert (raw.commits, raw.rollbacks) == (1, 0)
    short_deadline.warning.assert_called_once()


@pytest.mark.asyncio
async def test_both_families_share_one_deadline(
    callback_scope, monkeypatch: pytest.MonkeyPatch, short_deadline: Mock
) -> None:
    # Given both families stalled
    _, events = callback_scope
    monkeypatch.setattr(access_cache, "flush_deferred_acl", _never)
    monkeypatch.setattr(redis_cache, "flush_deferred_versions", _never)
    # When
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with connection_ctx():
        await _queue_cache_writes()
        session_module.run_after_commit(lambda: events.append("launched"))
    # Then a single wait bounded the flush, not one per family
    assert loop.time() - started < 0.5
    assert events == ["commit", "launched"]
    short_deadline.warning.assert_called_once()


@pytest.mark.asyncio
async def test_redis_failure_after_commit_keeps_commit_and_launches_callbacks(
    callback_scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a Redis that rejects the deferred version batch
    raw, events = callback_scope
    cache = _install_cache(monkeypatch)
    cache.fail = True
    monkeypatch.setattr(redis_cache, "flush_deferred_versions", _REAL_FLUSH_VERSIONS)
    # When
    async with connection_ctx():
        await redis_cache.bump_version("ver")
        session_module.run_after_commit(lambda: events.append("launched"), lambda: events.append("dropped"))
    # Then
    assert events == ["commit", "acl", "launched"]
    assert (raw.commits, raw.rollbacks) == (1, 0)


@pytest.mark.asyncio
async def test_cancellation_during_flush_propagates_and_still_launches(
    callback_scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a flush that is in flight when the request task is cancelled
    raw, events = callback_scope
    in_flight = asyncio.Event()

    async def stall() -> None:
        in_flight.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(redis_cache, "flush_deferred_versions", stall)

    async def scope() -> None:
        async with connection_ctx():
            await _queue_cache_writes()
            session_module.run_after_commit(lambda: events.append("launched"), lambda: events.append("dropped"))

    task = asyncio.create_task(scope())
    # When
    await in_flight.wait()
    _ = task.cancel()
    # Then
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["commit", "acl", "launched"]
    assert raw.commits == 1


@pytest.fixture
def guest_write_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CountingPool:
    pool = CountingPool()
    monkeypatch.setattr(session_module, "get_pool", lambda: pool)
    monkeypatch.setattr(exclusion.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    monkeypatch.setattr(exclusion, "assert_no_active_run", AsyncMock())
    monkeypatch.setattr(exclusion, "assert_transfer_ready", AsyncMock())
    return pool


@pytest.mark.asyncio
async def test_guest_write_flock_wait_times_out_with_no_checkout_held(
    guest_write_pool: CountingPool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given an exclusive cutover holder and a short shared-lock wait budget
    agent_id = uuid4()
    waits: list[int] = []
    real_sleep = agent_setup_lock.anyio.sleep

    async def observed_sleep(delay: float) -> None:
        waits.append(guest_write_pool.active)
        await real_sleep(delay)

    monkeypatch.setattr(exclusion, "GUEST_WRITE_LOCK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(agent_setup_lock.anyio, "sleep", observed_sleep)

    # When the writer waits out GUEST_WRITE_LOCK_TIMEOUT_SECONDS behind the holder
    async with setup_workspace_lock(tmp_path / str(agent_id)):
        with pytest.raises(WorkspaceLockTimeoutError):
            async with exclusion.guest_write_section(agent_id):
                pytest.fail("a contended writer must not enter")

    # Then every poll of the flock ran with the pool slot free, after one committed gate scope
    assert waits
    assert set(waits) == {0}
    assert (guest_write_pool.active, guest_write_pool.checkouts, guest_write_pool.releases) == (0, 1, 1)
    assert (guest_write_pool.raw.commits, guest_write_pool.raw.rollbacks) == (1, 0)


@pytest.mark.asyncio
async def test_guest_write_section_checks_in_short_scopes_around_the_flock(
    guest_write_pool: CountingPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given gate checks that register callbacks with the scope they run in
    effects: list[tuple[str, int, int]] = []

    def after_commit(name: str) -> None:
        session_module.run_after_commit(
            lambda: effects.append((name, guest_write_pool.raw.commits, guest_write_pool.active))
        )

    checks = iter(["before the lock", "under the lock"])

    async def gate(_agent_id: UUID) -> None:
        after_commit(next(checks))

    async def transfer(_agent_id: UUID) -> None:
        after_commit("transfer")

    monkeypatch.setattr(exclusion, "assert_no_active_run", gate)
    monkeypatch.setattr(exclusion, "assert_transfer_ready", transfer)

    # When the section runs its body
    async with exclusion.guest_write_section(uuid4()):
        effects.append(("body", guest_write_pool.raw.commits, guest_write_pool.active))

    # Then each callback fires only after its own scope commits, and the body holds no checkout
    assert effects == [("before the lock", 1, 1), ("under the lock", 2, 1), ("transfer", 2, 1), ("body", 2, 0)]
    assert (guest_write_pool.checkouts, guest_write_pool.releases) == (2, 2)


@pytest.mark.asyncio
async def test_guest_write_exclusion_authorizes_in_one_scope_then_holds_no_checkout(
    guest_write_pool: CountingPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an access check that must run inside a scope
    seen: list[int] = []

    async def access(_user: UserRecord, _agent_id: UUID) -> tuple[None, str]:
        seen.append(guest_write_pool.active)
        return None, "use"

    monkeypatch.setattr(exclusion, "check_agent_access", access)

    # When the dependency is suspended at its yield, as it is while the handler runs
    dependency = exclusion.guest_write_exclusion(uuid4(), UserRecord(id=uuid4()))
    await anext(dependency)

    # Then authorization held the slot and the handler does not
    assert seen == [1]
    assert guest_write_pool.active == 0
    await dependency.aclose()
    assert (guest_write_pool.checkouts, guest_write_pool.releases) == (3, 3)
