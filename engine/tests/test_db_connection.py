"""Unit tests for DbConnection against an in-memory fake psycopg connection."""

from __future__ import annotations

from typing import Any

import pytest

from app.db.connection import DbConnection
from app.db.errors import DbError, UniqueViolationError, map_psycopg_error


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent
        self._rows: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self._parent.executed.append((query, params))
        if self._parent.fail_with is not None:
            raise self._parent.fail_with
        self._rows = list(self._parent.next_rows)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeRawConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.next_rows: list[dict[str, Any]] = []
        self.fail_with: BaseException | None = None
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_fetchone_and_fetchall_return_dicts() -> None:
    # Given
    raw = _FakeRawConnection()
    raw.next_rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    db = DbConnection(raw)  # type: ignore[arg-type]

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
    db = DbConnection(raw)  # type: ignore[arg-type]

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
    db = DbConnection(raw)  # type: ignore[arg-type]

    with pytest.raises(DbError):
        await db.execute("SELECT 1")


def test_map_psycopg_unique_violation() -> None:
    psycopg_errors = pytest.importorskip("psycopg.errors")

    class _FakeUnique(psycopg_errors.UniqueViolation):
        @property
        def diag(self):  # type: ignore[override]
            return type("Diag", (), {"constraint_name": "users_email_key"})()

    mapped = map_psycopg_error(_FakeUnique("duplicate"))
    assert isinstance(mapped, UniqueViolationError)
    assert mapped.constraint == "users_email_key"
