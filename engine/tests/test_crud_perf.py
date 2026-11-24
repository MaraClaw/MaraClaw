"""CRUD performance contracts, with opt-in PostgreSQL for message ordering."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

from app.dao.chat_dao import chat_message_dao
from app.db import session as session_module
from app.db.connection import DbConnection
from app.services import tool_config as tool_config_mod
from app.services.trigger_runtime import executions as trigger_executions


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> Self:
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
async def test_list_latest_for_conversations_bounds_each_unique_id_in_one_query(raw: _FakeRawConnection) -> None:
    grouped = await chat_message_dao.list_latest_for_conversations(
        conversation_ids=["a", "b", "a"],
        limit=3,
    )
    assert grouped == {"a": [], "b": []}
    assert len(raw.executed) == 1
    query, params = raw.executed[0]
    assert "CROSS JOIN LATERAL" in query
    assert "LIMIT %(limit)s" in query
    assert params["ids"] == ["a", "b"]
    assert params["limit"] == 3


@pytest.mark.parametrize(("ids", "limit"), [([], 3), (["a"], 0), (["a"], -1)])
@pytest.mark.asyncio
async def test_list_latest_for_conversations_skips_empty_requests(
    raw: _FakeRawConnection, ids: list[str], limit: int
) -> None:
    assert await chat_message_dao.list_latest_for_conversations(conversation_ids=ids, limit=limit) == {}
    assert not raw.executed


@pytest.mark.parametrize(
    ("limit", "z_ids", "a_ids"),
    [(1, [2], [6]), (3, [2, 8, 4], [6, 5]), (2**63, [2, 8, 4, 3, 1], [6, 5])],
)
@pytest.mark.asyncio
async def test_list_latest_for_conversations_groups_with_stable_ties(
    monkeypatch: pytest.MonkeyPatch, limit: int, z_ids: list[int], a_ids: list[int]
) -> None:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("Set MARACLAW_TEST_POSTGRES_DSN to run isolated temporary-table Postgres checks")
    async with await AsyncConnection[DictRow].connect(dsn, row_factory=dict_row) as conn:
        db = DbConnection(conn)
        await db.execute("""
            CREATE TEMP TABLE chat_messages (
                id uuid PRIMARY KEY, agent_id uuid NOT NULL, user_id uuid NOT NULL, role text NOT NULL,
                content text NOT NULL, conversation_id text NOT NULL, participant_id uuid, thinking text,
                client_submission_id uuid, submission_fingerprint text, attachments jsonb NOT NULL DEFAULT '[]',
                created_at timestamptz NOT NULL
            )
        """)
        await db.execute("""
            INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, created_at)
            SELECT lpad(n::text, 32, '0')::uuid, md5('agent')::uuid, md5('user')::uuid,
                   'assistant', 'message ' || n, conversation_id, to_timestamp(ts)
            FROM (VALUES (1, 'z', 1), (3, 'z', 2), (8, 'z', 2), (4, 'z', 2), (2, 'z', 3),
                         (5, 'a', 1), (6, 'a', 2), (7, '', 1), (9, 'unrequested', 4)) AS seed(n, conversation_id, ts)
        """)

        @asynccontextmanager
        async def session() -> AsyncGenerator[DbConnection]:
            yield db

        monkeypatch.setattr(chat_message_dao, "session", session)
        grouped = await chat_message_dao.list_latest_for_conversations(
            conversation_ids=["z", "missing", "a", "z", "", "a"], limit=limit
        )

    assert [(key, [message.id for message in messages]) for key, messages in grouped.items()] == [
        ("z", [UUID(int=n) for n in z_ids]),
        ("missing", []),
        ("a", [UUID(int=n) for n in a_ids]),
        ("", []),
        ("web", [UUID(int=7)]),
    ]


@pytest.mark.asyncio
async def test_get_tenant_tool_configs_one_round_trip(raw: _FakeRawConnection) -> None:
    tools: list[Any] = [
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

    claimed = await trigger_executions.claim_pending_trigger_executions(max_agents=1, limit=10)
    assert len(claimed) == 1
    assert claimed[0][0].status == "processing"
    updates = [q for q, _p in raw.executed if q.lstrip().startswith("UPDATE trigger_executions")]
    assert len(updates) == 1
    assert "ANY(%(ids)s)" in updates[0]
