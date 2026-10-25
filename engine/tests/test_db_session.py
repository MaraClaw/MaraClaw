"""Tests for context-bound psycopg session helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextvars import Context
from typing import Any, Self, cast
from unittest.mock import Mock

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.core import access_cache, redis_cache
from app.db import pool as pool_module, session as session_module
from app.db.connection import DbConnection
from app.db.session import (
    bind_crud_connection,
    connection_ctx,
    flush_request_transaction,
    get_connection,
    transaction,
)
from transaction_pool_fakes import CountingPool


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

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
        self._parent.executed.append((str(query), params))

    async def fetchone(self) -> None:
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
    publications: list[tuple[int, tuple[str, ...]]] = []
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async def delete_now(*keys: str) -> None:
        publications.append((raw.commits, keys))

    monkeypatch.setattr(redis_cache, "_delete_now", delete_now)
    async with connection_ctx():
        await redis_cache.cache_delete("snapshot", defer_until_commit=True)
        await redis_cache.cache_delete("snapshot", defer_until_commit=True)
        assert publications == []
        # When
        await flush_request_transaction()
        # Then
        assert publications == [(1, ("snapshot",))]
    assert publications == [(1, ("snapshot",))]


@pytest.mark.asyncio
async def test_snapshot_deletes_are_discarded_after_request_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    raw = _FakeRawConnection()
    publications: list[str] = []
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))

    async def delete_now(*keys: str) -> None:
        publications.extend(keys)

    monkeypatch.setattr(redis_cache, "_delete_now", delete_now)

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
    assert publications == []
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
    # When
    with pytest.raises(type(failure)):
        async with connection_ctx():
            session_module.run_after_commit(lambda: events.append("launched"), lambda: events.append("dropped"))
    # Then
    assert events == ["commit", "launched"]


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
async def test_after_commit_nested_joins_share_outer_batch(callback_scope) -> None:
    # Given
    _, events = callback_scope
    # When
    async with connection_ctx():
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
