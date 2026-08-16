"""Tests for context-bound psycopg session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.db import pool as pool_module, session as session_module
from app.db.connection import DbConnection
from app.db.session import bind_crud_connection, connection_ctx, get_connection, transaction


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeCursor:
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
    async def __aenter__(self) -> _FakeTransaction:
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
    return DbConnection(cast(AsyncConnection[DictRow], raw))


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
