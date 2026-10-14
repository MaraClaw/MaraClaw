"""Gateway regression coverage: Feishu channel resolution, delivery timeout, guarded delivery SQL."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import gateway
from app.config import Settings
from app.dao.gateway_message_dao import gateway_message_dao
from app.db import session as session_module
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.org import OrgMemberRecord
from app.schemas.schemas import GatewaySendMessageRequest


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self._parent.executed.append((query, params))

    async def fetchone(self) -> dict[str, Any] | None:
        return self._parent.rows[0] if self._parent.rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._parent.rows)


class _FakeRawConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
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


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeRawConnection]:
    conn = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(conn))
    token = session_module._conn_ctx.set(None)
    yield conn
    session_module._conn_ctx.reset(token)


def _row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "sender_agent_id": None,
        "sender_user_id": None,
        "conversation_id": None,
        "content": "hello",
        "status": "delivered",
        "result": None,
        "selected_slot": None,
        "guest_model_ref": None,
        "complexity": None,
        "routing_reason": None,
        "created_at": datetime(2026, 10, 14, 9, 0, tzinfo=UTC),
        "delivered_at": datetime(2026, 10, 14, 9, 1, tzinfo=UTC),
        "completed_at": None,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_claim_pending_moves_only_pending_rows_in_one_statement(raw: _FakeRawConnection) -> None:
    agent_id = uuid.uuid4()
    now = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    raw.rows = [_row(agent_id=agent_id, delivered_at=now)]

    claimed = await gateway_message_dao.claim_pending(agent_id, now=now)

    assert [row.status for row in claimed] == ["delivered"]
    assert len(raw.executed) == 1
    query, params = raw.executed[0]
    assert query.startswith("WITH claimed AS (UPDATE gateway_messages SET status = 'delivered'")
    assert "WHERE agent_id = %(agent_id)s AND status = 'pending' RETURNING" in query
    assert query.endswith("FROM claimed ORDER BY created_at ASC")
    assert params == {"agent_id": agent_id, "now": now}


@pytest.mark.asyncio
async def test_complete_is_guarded_by_open_status(raw: _FakeRawConnection) -> None:
    message_id, agent_id = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)

    assert await gateway_message_dao.complete(message_id, agent_id, result="done", now=now) is None

    query, params = raw.executed[0]
    assert query.startswith("UPDATE gateway_messages SET status = 'completed', result = %(result)s")
    assert "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status IN ('pending', 'delivered')" in query
    assert params == {"id": message_id, "agent_id": agent_id, "result": "done", "now": now}


@pytest.mark.asyncio
async def test_requeue_stale_deliveries_never_touches_completed_rows(raw: _FakeRawConnection) -> None:
    agent_id = uuid.uuid4()
    stale_before = datetime(2026, 10, 14, 9, 45, tzinfo=UTC)
    requeued_id = uuid.uuid4()
    raw.rows = [{"id": requeued_id}]

    assert await gateway_message_dao.requeue_stale_deliveries(agent_id, stale_before=stale_before) == [requeued_id]

    query, params = raw.executed[0]
    assert query.startswith("UPDATE gateway_messages SET status = 'pending', delivered_at = NULL")
    assert "AND status = 'delivered' AND (delivered_at IS NULL OR delivered_at < %(stale_before)s)" in query
    assert params == {"agent_id": agent_id, "stale_before": stale_before}


@pytest.mark.asyncio
async def test_release_delivery_requires_the_same_claim(raw: _FakeRawConnection) -> None:
    claimed = GatewayMessageRecord(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        content="hello",
        status="delivered",
        delivered_at=datetime(2026, 10, 14, 9, 1, tzinfo=UTC),
    )

    assert await gateway_message_dao.release_delivery(claimed) is None

    query, params = raw.executed[0]
    assert "AND status = 'delivered' AND delivered_at = %(delivered_at)s" in query
    assert params == {"id": claimed.id, "agent_id": claimed.agent_id, "delivered_at": claimed.delivered_at}


@pytest.mark.asyncio
async def test_mark_delivered_claims_only_a_pending_row(raw: _FakeRawConnection) -> None:
    message_id, agent_id = uuid.uuid4(), uuid.uuid4()

    assert await gateway_message_dao.mark_delivered(message_id, agent_id) is None

    query, params = raw.executed[0]
    assert query.startswith("UPDATE gateway_messages SET status = 'delivered', delivered_at = %(now)s")
    assert "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status = 'pending' RETURNING" in query
    assert (params["id"], params["agent_id"]) == (message_id, agent_id)


def test_delivery_timeout_setting_must_be_positive() -> None:
    assert Settings(OPENCLAW_DELIVERY_TIMEOUT_SECONDS=1).OPENCLAW_DELIVERY_TIMEOUT_SECONDS == 1
    for value in (0, -5):
        with pytest.raises(ValidationError):
            _ = Settings(OPENCLAW_DELIVERY_TIMEOUT_SECONDS=value)


@pytest.mark.asyncio
async def test_send_message_rejects_cross_tenant_feishu_fallback_when_no_tenant_config_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    source_agent = AgentRecord(
        id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        name="Sending agent",
        tenant_id=tenant_id,
    )
    member = OrgMemberRecord(
        id=uuid.uuid4(),
        name="Recipient",
        external_id="ou_recipient",
        status="active",
        tenant_id=tenant_id,
    )
    relationship = SimpleNamespace(
        agent_id=source_agent.id,
        member_id=member.id,
        member=member,
        relation="collaborator",
        description="",
    )

    agent_lookup_calls: list[uuid.UUID] = []
    tenant_lookup_calls: list[tuple[uuid.UUID, str]] = []

    async def get_source_agent(_api_key: str) -> AgentRecord:
        return source_agent

    async def active_human_relationship(
        _relationship,
        *,
        source_agent: AgentRecord | None = None,
    ) -> dict[str, str]:
        _ = source_agent
        return {"access_status": "active"}

    async def no_agent_rels(_agent_id: uuid.UUID):
        return []

    async def human_rels(_agent_id: uuid.UUID):
        return [relationship]

    async def no_agent_feishu(*, agent_id: uuid.UUID, channel_type: str):
        agent_lookup_calls.append(agent_id)
        assert channel_type == "feishu"
        return

    async def no_tenant_feishu(*, tenant_id: uuid.UUID, channel_type: str):
        tenant_lookup_calls.append((tenant_id, channel_type))
        return

    async def touch_last_seen(*, db_obj, obj_in):
        return db_obj

    monkeypatch.setattr(gateway, "_get_agent_by_key", get_source_agent)
    monkeypatch.setattr(gateway, "evaluate_human_relationship_status", active_human_relationship)
    monkeypatch.setattr(gateway.agent_agent_relationship_dao, "list_for_agent_with_targets", no_agent_rels)
    monkeypatch.setattr(gateway.agent_relationship_dao, "list_for_agent_with_members", human_rels)
    monkeypatch.setattr(gateway.channel_config_dao, "get_for_agent", no_agent_feishu)
    monkeypatch.setattr(gateway.channel_config_dao, "get_for_tenant_channel", no_tenant_feishu)
    monkeypatch.setattr(gateway.agent_dao, "update", touch_last_seen)

    with pytest.raises(HTTPException) as error:
        await gateway.send_message(
            GatewaySendMessageRequest(target=member.name, content="Hello"),
            "api-key",
        )

    assert error.value.status_code == 400
    assert error.value.detail == "No Feishu channel configured"
    assert agent_lookup_calls == [source_agent.id]
    assert tenant_lookup_calls == [(tenant_id, "feishu")]
