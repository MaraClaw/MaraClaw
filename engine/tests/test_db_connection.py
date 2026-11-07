"""Unit tests for DbConnection against an in-memory fake psycopg connection."""

from __future__ import annotations

from typing import Any, Self, cast

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.db.connection import DbConnection
from app.db.errors import (
    DbError,
    PoolCheckoutTimeoutError,
    QueryCanceledError,
    UniqueViolationError,
    map_psycopg_error,
    overload_error,
)


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent
        self._rows: list[dict[str, Any]] = []

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
        if self._parent.fail_with is not None:
            raise self._parent.fail_with
        self._rows = list(self._parent.next_rows)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

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
        self.next_rows: list[dict[str, Any]] = []
        self.fail_with: BaseException | None = None
        self.commits = 0
        self.rollbacks = 0
        self.finish_with: BaseException | None = None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        self.commits += 1
        if self.finish_with is not None:
            raise self.finish_with

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self.finish_with is not None:
            raise self.finish_with

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


def _wrap(raw: _FakeRawConnection) -> DbConnection:
    return DbConnection(cast("AsyncConnection[DictRow]", raw))


@pytest.mark.asyncio
async def test_fetchone_and_fetchall_return_dicts() -> None:
    # Given
    raw = _FakeRawConnection()
    raw.next_rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    db = _wrap(raw)

    # When
    one = await db.fetchone("SELECT id, name FROM t WHERE id = %(id)s", {"id": 1})
    many = await db.fetchall("SELECT id, name FROM t")

    # Then
    assert one == {"id": 1, "name": "a"}
    assert many == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert raw.executed[0][0].startswith("SELECT id, name FROM t WHERE id")
    assert raw.executed[0][1] == {"id": 1}


@pytest.mark.asyncio
async def test_fetchval_by_name_and_index() -> None:
    raw = _FakeRawConnection()
    raw.next_rows = [{"ok": 1, "label": "x"}]
    db = _wrap(raw)

    assert await db.fetchval("SELECT 1 AS ok", column="ok") == 1
    raw.next_rows = [{"ok": 1, "label": "x"}]
    assert await db.fetchval("SELECT 1 AS ok", column=0) == 1
    raw.next_rows = []
    assert await db.fetchval("SELECT 1") is None


@pytest.mark.asyncio
async def test_execute_maps_errors() -> None:
    raw = _FakeRawConnection()

    class _BoomError(Exception):
        pass

    raw.fail_with = _BoomError("nope")
    db = _wrap(raw)

    with pytest.raises(DbError):
        await db.execute("SELECT 1")


def test_map_psycopg_unique_violation() -> None:
    from types import SimpleNamespace

    from app.db.errors import _constraint_name

    psycopg_errors = pytest.importorskip("psycopg.errors")

    mapped = map_psycopg_error(psycopg_errors.UniqueViolation("duplicate"))
    assert isinstance(mapped, UniqueViolationError)
    assert mapped.orig is not None
    assert (
        _constraint_name(SimpleNamespace(diag=SimpleNamespace(constraint_name="users_email_key"))) == "users_email_key"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["execute", "fetchone"])
async def test_lock_not_available_maps_with_original(method: str) -> None:
    # Given
    from psycopg.errors import LockNotAvailable

    from app.db.errors import LockNotAvailableError

    raw = _FakeRawConnection()
    original = LockNotAvailable("busy")
    assert original.sqlstate == "55P03"
    raw.fail_with = original
    db = _wrap(raw)

    # When / Then
    with pytest.raises(LockNotAvailableError) as caught:
        await getattr(db, method)("SELECT 1")
    assert caught.value.orig is original


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["execute", "fetchone", "fetchall"])
async def test_query_canceled_maps_to_typed_overload(method: str) -> None:
    # Given
    from psycopg.errors import QueryCanceled

    raw = _FakeRawConnection()
    original = QueryCanceled("canceling statement due to statement timeout")
    assert original.sqlstate == "57014"
    raw.fail_with = original

    # When / Then
    with pytest.raises(QueryCanceledError) as caught:
        await getattr(_wrap(raw), method)("SELECT pg_sleep(60)")
    assert caught.value.orig is original


def test_overload_error_classifies_raw_wrapped_and_unrelated() -> None:
    # Given
    from psycopg.errors import LockNotAvailable, QueryCanceled
    from psycopg_pool import PoolTimeout

    pool_timeout = PoolTimeout("couldn't get a connection after 30.00 sec")
    canceled = QueryCanceled("canceled")

    # Then
    assert isinstance(overload_error(pool_timeout), PoolCheckoutTimeoutError)
    assert isinstance(overload_error(canceled), QueryCanceledError)
    wrapped = overload_error(DbError("wrapped", orig=canceled))
    assert isinstance(wrapped, QueryCanceledError)
    assert wrapped.orig is canceled
    assert isinstance(overload_error(DbError("wrapped", orig=pool_timeout)), PoolCheckoutTimeoutError)
    typed = QueryCanceledError(orig=canceled)
    assert overload_error(typed) is typed
    assert overload_error(DbError("plain")) is None
    assert overload_error(LockNotAvailable("busy")) is None
    assert overload_error(RuntimeError("boom")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["commit", "rollback"])
async def test_commit_and_rollback_map_overload_only(method: str) -> None:
    # Given
    from psycopg.errors import QueryCanceled, UniqueViolation

    raw = _FakeRawConnection()
    db = _wrap(raw)
    canceled = QueryCanceled("canceled")
    raw.finish_with = canceled

    # When / Then
    with pytest.raises(QueryCanceledError) as caught:
        await getattr(db, method)()
    assert caught.value.orig is canceled
    assert caught.value.__cause__ is canceled

    other = UniqueViolation("duplicate")
    raw.finish_with = other
    with pytest.raises(UniqueViolation) as raw_caught:
        await getattr(db, method)()
    assert raw_caught.value is other
