"""Tests for psycopg BaseDAO connection context and CRUD helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from app.dao.base import BaseDAO
from app.db import session as session_module
from app.db.connection import DbConnection
from app.db.session import get_connection


@dataclass
class _Item:
    id: Any
    name: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> _Item:
        return cls(id=row["id"], name=row["name"])


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
        self._rows = list(self._parent.next_rows)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeRawConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.next_rows: list[dict[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


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


class ItemDAO(BaseDAO[_Item]):
    table = "items"
    columns = ("id", "name")
    record_factory = staticmethod(_Item.from_row)


@pytest.fixture(autouse=True)
def _clear_ctx():
    token = session_module._conn_ctx.set(None)
    yield
    session_module._conn_ctx.reset(token)


@pytest.mark.asyncio
async def test_standalone_dao_session_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    dao = ItemDAO()

    async with dao.session() as db:
        assert isinstance(db, DbConnection)
        assert get_connection() is db

    assert raw.commits == 1
    assert raw.rollbacks == 0
    assert get_connection() is None


@pytest.mark.asyncio
async def test_standalone_dao_session_rolls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    dao = ItemDAO()

    async def _boom() -> None:
        async with dao.session():
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _boom()

    assert raw.commits == 0
    assert raw.rollbacks == 1


@pytest.mark.asyncio
async def test_delete_returns_deleted_row(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    item_id = uuid4()
    raw.next_rows = [{"id": item_id, "name": "gone"}]
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    dao = ItemDAO()

    deleted = await dao.delete(id=item_id)

    assert deleted is not None
    assert deleted.id == item_id
    assert deleted.name == "gone"
    assert any("DELETE FROM items" in q for q, _ in raw.executed)
    assert raw.commits == 1


@pytest.mark.asyncio
async def test_get_many_uses_any_and_skips_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    item_id = uuid4()
    raw.next_rows = [{"id": item_id, "name": "kept"}]
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    dao = ItemDAO()

    assert await dao.get_many([]) == []
    rows = await dao.get_many([item_id])

    assert len(rows) == 1
    assert rows[0].name == "kept"
    query, params = raw.executed[0]
    assert "ANY(%(ids)s)" in query
    assert params["ids"] == [item_id]
