"""SQL-shape tests for CRUD performance changes (no live Postgres)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest

from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db import session as session_module
from app.services import tool_config as tool_config_mod
from app.services.trigger_runtime import executions as trigger_executions


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self._parent.executed.append((query, params))

    async def fetchone(self) -> None:
        return None

    async def fetchall(self) -> list[Any]:
        return []


class _FakeRawConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        self.commits += 1

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


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeRawConnection]:
    conn = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(conn))
    token = session_module._conn_ctx.set(None)
    yield conn
    session_module._conn_ctx.reset(token)


@pytest.mark.asyncio
async def test_find_best_web_session_scopes_message_aggregate(raw: _FakeRawConnection) -> None:
    await chat_session_dao.find_best_web_session(agent_id=uuid4(), user_id=uuid4())
    query, _params = raw.executed[0]
    assert "FROM chat_messages" in query
    assert "WHERE conversation_id IN" in query
    assert "source_channel = 'web'" in query
    assert query.count("FROM chat_messages") == 1


@pytest.mark.asyncio
async def test_list_latest_for_conversations_uses_window(raw: _FakeRawConnection) -> None:
    grouped = await chat_message_dao.list_latest_for_conversations(
        conversation_ids=["a", "b"],
        limit=3,
    )
    assert grouped == {"a": [], "b": []}
    query, params = raw.executed[0]
    assert "ROW_NUMBER()" in query
    assert "ANY(%(ids)s)" in query
    assert params["ids"] == ["a", "b"]
    assert params["limit"] == 3


@pytest.mark.asyncio
async def test_get_tenant_tool_configs_one_round_trip(raw: _FakeRawConnection) -> None:
    tools = [
        type("T", (), {"name": "search_web", "source": "builtin", "config_schema": None})(),
        type("T", (), {"name": "read_file", "source": "builtin", "config_schema": None})(),
    ]
    await tool_config_mod.get_tenant_tool_configs(uuid4(), tools)
    assert len(raw.executed) == 1
    query, params = raw.executed[0]
    assert "FROM tenant_settings" in query
    assert "ANY(%(keys)s)" in query
    assert "tool_config:search_web" in params["keys"]
    assert "tool_config:read_file" in params["keys"]


@pytest.mark.asyncio
async def test_claim_pending_updates_all_ids_in_one_statement(
    raw: _FakeRawConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_id = uuid4()
    execution = type("E", (), {"id": exec_id, "started_at": None})()
    trigger = type("T", (), {})()

    async def fake_claim(**_kwargs):
        return [(execution, trigger)]

    monkeypatch.setattr(trigger_executions.trigger_execution_dao, "claim_pending", fake_claim)
    monkeypatch.setattr(trigger_executions.settings, "INSTANCE_ID", "test-node")

    claimed = await trigger_executions.claim_pending_trigger_executions(limit=10)
    assert len(claimed) == 1
    assert claimed[0][0].status == "processing"
    updates = [q for q, _p in raw.executed if q.lstrip().startswith("UPDATE trigger_executions")]
    assert len(updates) == 1
    assert "ANY(%(ids)s)" in updates[0]
