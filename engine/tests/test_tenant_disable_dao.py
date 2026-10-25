"""SQL shape for tenant-disable bulk updates (no live Postgres)."""

from __future__ import annotations

from typing import Any, Self
from uuid import uuid4

import pytest

from app.dao.agent_dao import agent_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.dao.user_dao import user_dao
from app.db import session as session_module


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self._parent.executed.append((query, params))

    async def fetchone(self) -> dict[str, object]:
        _query, params = self._parent.executed[-1]
        return {"id": params["id"]}

    async def fetchall(self) -> list[dict[str, Any]]:
        return [{"id": uuid4()}]


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
async def test_deactivate_for_tenant_skips_platform_admin(monkeypatch):
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    tenant_id = uuid4()
    await user_dao.deactivate_for_tenant(tenant_id)
    lock_query, lock_params = raw.executed[0]
    assert "FROM tenants" in lock_query
    assert "FOR UPDATE" in lock_query
    assert lock_params["id"] == tenant_id
    query, params = next((query, params) for query, params in raw.executed if query.lstrip().startswith("UPDATE users"))
    assert "role <> %(platform_admin)s" in query
    assert params["tenant_id"] == tenant_id
    assert params["platform_admin"] == "platform_admin"


@pytest.mark.asyncio
async def test_disable_agents_stops_not_paused(monkeypatch):
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    tenant_id = uuid4()
    await agent_dao.disable_for_tenant(tenant_id)
    query, params = raw.executed[0]
    assert "status = 'stopped'" in query
    assert "heartbeat_enabled = FALSE" in query
    assert "paused" not in query
    assert params["tenant_id"] == tenant_id


@pytest.mark.asyncio
async def test_disable_triggers_and_schedules_scope_to_tenant(monkeypatch):
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    tenant_id = uuid4()
    await agent_trigger_dao.disable_for_tenant(tenant_id)
    await agent_schedule_dao.disable_for_tenant(tenant_id)
    trigger_sql = raw.executed[0][0]
    schedule_sql = raw.executed[1][0]
    assert "FROM agents WHERE tenant_id = %(tenant_id)s" in trigger_sql
    assert "SET is_enabled = FALSE, self_disabled = FALSE" in trigger_sql
    assert "WHERE (is_enabled IS TRUE OR self_disabled IS TRUE) AND agent_id IN (" in trigger_sql
    assert "FROM agents WHERE tenant_id = %(tenant_id)s" in schedule_sql
    assert "next_run_at = NULL" in schedule_sql


@pytest.mark.parametrize("scoped", [False, True])
async def test_disable_creator_clears_provenance_in_scope(monkeypatch, scoped):
    # Given a creator and an optional tenant boundary.
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    creator_id, tenant_id = uuid4(), uuid4() if scoped else None
    # When disabling the creator's triggers.
    count = await agent_trigger_dao.disable_for_creator(creator_id, tenant_id=tenant_id)
    # Then enabled and self-disabled rows are cleared within that boundary.
    query, params = raw.executed[0]
    assert "SET is_enabled = FALSE, self_disabled = FALSE" in query
    assert "WHERE (is_enabled IS TRUE OR self_disabled IS TRUE) AND agent_id IN (" in query
    assert "FROM agents WHERE creator_id = %(creator_id)s" in query
    assert ("AND tenant_id = %(tenant_id)s" in query) is scoped
    assert params == ({"creator_id": creator_id, "tenant_id": tenant_id} if scoped else {"creator_id": creator_id})
    assert count == 1
