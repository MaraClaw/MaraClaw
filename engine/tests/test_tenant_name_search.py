"""Tenant name full-text search helpers and SQL shape."""

from __future__ import annotations

from typing import Any

import pytest

from app.dao.tenant_dao import tenant_dao, tenant_name_tsquery
from app.db import session as session_module


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("   ", None),
        ("!!!", None),
        ("mara", "mara:*"),
        ("  Mara Claw ", "mara:* & claw:*"),
        ("techadmin@marathon.vn", "techadmin:* & marathon:* & vn:*"),
    ],
)
def test_tenant_name_tsquery(raw: str, expected: str | None) -> None:
    assert tenant_name_tsquery(raw) == expected


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self._parent.executed.append((query, params))

    async def fetchall(self) -> list[dict[str, Any]]:
        return []


class _FakeRawConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


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
def _clear_ctx():
    token = session_module._conn_ctx.set(None)
    yield
    session_module._conn_ctx.reset(token)


@pytest.mark.asyncio
async def test_search_by_name_uses_prefix_tsquery(monkeypatch):
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    await tenant_dao.search_by_name("Mara")
    query, params = raw.executed[0]
    assert "name_tsv @@ to_tsquery('simple', %(query)s)" in query
    assert "ts_rank_cd(name_tsv, to_tsquery('simple', %(query)s))" in query
    assert params["query"] == "mara:*"
    assert params["limit"] == 50


@pytest.mark.asyncio
async def test_search_by_name_blank_falls_back_to_list(monkeypatch):
    listed = object()
    called = {"n": 0}

    async def _list(*, desc: bool = True):
        called["n"] += 1
        assert desc is True
        return listed

    monkeypatch.setattr(tenant_dao, "list_ordered_by_created_at", _list)
    result = await tenant_dao.search_by_name("   ")
    assert result is listed
    assert called["n"] == 1
