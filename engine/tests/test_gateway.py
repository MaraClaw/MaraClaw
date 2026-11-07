"""Gateway regression coverage: Feishu channel resolution, guarded delivery SQL, idempotent reports."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import gateway
from app.config import get_settings
from app.core.logging import configure_logging
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.llm_dao import llm_model_dao
from app.db import session as session_module
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.llm import LLMModelRecord
from app.records.openclaw_dispatch import SharedDispatchRequest
from app.records.org import OrgMemberRecord
from app.schemas.schemas import GatewayReportRequest, GatewaySendMessageRequest
from app.services import openclaw_gateway_delivery as delivery


class _FakeCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> Self:
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
async def test_claim_ordinary_head_is_scoped_to_message_and_agent(raw: _FakeRawConnection) -> None:
    agent_id, message_id = uuid.uuid4(), uuid.uuid4()
    raw.rows = [_row(id=message_id, agent_id=agent_id)]
    claimed = await gateway_message_dao.claim_ordinary_head(message_id, agent_id)
    assert claimed is not None
    assert (claimed.id, claimed.agent_id, claimed.status) == (message_id, agent_id, "delivered")
    assert raw.executed[0][1] == {"id": message_id, "agent": agent_id}


@pytest.mark.asyncio
async def test_complete_is_guarded_by_open_status(raw: _FakeRawConnection) -> None:
    message_id, agent_id = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)

    assert await gateway_message_dao.complete(message_id, agent_id, result="done", now=now) is None

    query, params = raw.executed[0]
    assert query.startswith("UPDATE gateway_messages SET status = 'completed', result = %(result)s")
    assert "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status IN ('pending', 'delivered')" in query
    assert params == {"id": message_id, "agent_id": agent_id, "result": "done", "now": now, "binding_id": None}


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

    async def no_tenant_feishu(*, tenant_id: uuid.UUID, channel_type: str):
        tenant_lookup_calls.append((tenant_id, channel_type))

    async def touch_last_seen(*, db_obj, obj_in):
        return db_obj

    @asynccontextmanager
    async def send_context():
        yield None

    async def admit(record):
        return record

    monkeypatch.setattr(gateway, "connection_ctx", send_context)
    monkeypatch.setattr(gateway.agent_dao, "lock_openclaw", admit)
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


class _DeliveryStore:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, GatewayMessageRecord] = {}
        self.intents: dict[tuple[uuid.UUID, str, uuid.UUID], SimpleNamespace] = {}
        self.chat: list[dict[str, Any]] = []
        self.events: list[str] = []

    @asynccontextmanager
    async def connection_ctx(self) -> AsyncGenerator[None]:
        snapshot = {key: replace(row) for key, row in self.rows.items()}
        intents_snapshot, chat_snapshot = dict(self.intents), list(self.chat)
        self.events.append("enter")
        try:
            yield
        except Exception:
            self.rows, self.intents, self.chat = snapshot, intents_snapshot, chat_snapshot
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    async def create_intent(
        self,
        *,
        message_id: uuid.UUID,
        destination_kind: str,
        destination_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
    ) -> SimpleNamespace | None:
        assert self.events[-1] == "enter", "intents must be written inside the report transaction"
        key = (message_id, destination_kind, destination_id)
        if key in self.intents:
            return None
        record = SimpleNamespace(
            id=uuid.uuid4(),
            message_id=message_id,
            destination_kind=destination_kind,
            destination_id=destination_id,
            tenant_id=tenant_id,
        )
        self.intents[key] = record
        return record

    async def claim_ready_shared(self, request: SharedDispatchRequest) -> GatewayMessageRecord | None:
        return None

    async def complete(
        self, message_id: uuid.UUID, agent_id: uuid.UUID, *, result: str, now: datetime
    ) -> GatewayMessageRecord | None:
        row = self.rows.get(message_id)
        if row is None or row.agent_id != agent_id or row.status not in ("pending", "delivered"):
            return None
        row.status, row.result, row.completed_at = "completed", result, now
        return replace(row)

    async def get_for_agent(self, message_id: uuid.UUID, agent_id: uuid.UUID) -> GatewayMessageRecord | None:
        row = self.rows.get(message_id)
        return replace(row) if row and row.agent_id == agent_id else None


@pytest.fixture
def delivery_store(monkeypatch: pytest.MonkeyPatch) -> _DeliveryStore:
    store = _DeliveryStore()
    for name in ("complete", "get_for_agent"):
        monkeypatch.setattr(gateway_message_dao, name, getattr(store, name))
    monkeypatch.setattr(delivery, "connection_ctx", store.connection_ctx)
    return store


@pytest.fixture
def report_env(monkeypatch: pytest.MonkeyPatch, delivery_store: _DeliveryStore) -> SimpleNamespace:
    tenant_id = uuid.uuid4()
    agent = AgentRecord(id=uuid.uuid4(), creator_id=uuid.uuid4(), name="Receiver", tenant_id=tenant_id)
    sender = AgentRecord(id=uuid.uuid4(), creator_id=uuid.uuid4(), name="Sender", tenant_id=tenant_id)
    session = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=uuid.uuid4(), source_channel="feishu")
    at = datetime(2026, 10, 14, 10, tzinfo=UTC)
    row = GatewayMessageRecord(
        id=uuid.uuid4(),
        agent_id=agent.id,
        content="question",
        conversation_id=str(session.id),
        sender_user_id=session.user_id,
        sender_agent_id=sender.id,
        created_at=at,
    )
    delivery_store.rows[row.id] = row
    calls: dict[str, list[Any]] = {name: [] for name in ("insert", "ws", "touch", "participant")}
    direct_calls = []
    inserted_id = uuid.uuid4()

    async def direct_delivery(*args, **kwargs):
        direct_calls.append((args, kwargs))

    async def authenticate(_key):
        return agent

    async def promote(record, *, touch_last_seen=True):
        return replace(record, status="running")

    async def admit(record):
        return record

    @asynccontextmanager
    async def poll_context():
        yield None

    monkeypatch.setattr(gateway.agent_dao, "touch_openclaw", promote)
    monkeypatch.setattr(gateway.agent_dao, "lock_openclaw", admit)
    monkeypatch.setattr(gateway, "lock_tenants", AsyncMock())
    monkeypatch.setattr(gateway, "lock_user", AsyncMock())
    monkeypatch.setattr(gateway, "lock_agent", AsyncMock())
    monkeypatch.setattr(gateway, "connection_ctx", poll_context)

    async def insert(**kwargs):
        assert delivery_store.events[-1] == "enter"
        delivery_store.chat.append(kwargs)
        calls["insert"].append(kwargs)
        return ChatMessageRecord(id=inserted_id, **kwargs)

    async def touch(**kwargs):
        calls["touch"].append(kwargs)
        return kwargs["db_obj"]

    async def participant(*args):
        calls["participant"].append(args)
        return SimpleNamespace(id=uuid.uuid4())

    async def get_session(_id):
        return session

    async def get_sender(_id):
        return sender

    async def ws(*args, **kwargs):
        assert delivery_store.events[-1] == "commit"
        calls["ws"].append((args, kwargs))

    monkeypatch.setattr(gateway, "_get_agent_by_key", authenticate)
    monkeypatch.setattr(delivery, "_utcnow", lambda: at)
    monkeypatch.setattr(delivery.chat_message_dao, "insert_message", insert)
    monkeypatch.setattr(delivery.agent_dao, "update", touch)
    monkeypatch.setattr(delivery.agent_dao, "get", get_sender)
    monkeypatch.setattr(delivery.participant_dao, "get_by_type_ref", participant)
    monkeypatch.setattr(delivery.chat_session_dao, "get", get_session)
    monkeypatch.setattr(delivery.gateway_report_delivery_dao, "create_intent", delivery_store.create_intent)
    monkeypatch.setattr("app.api.websocket.manager", SimpleNamespace(send_to_session=ws))
    monkeypatch.setattr("app.services.channels.outbound.deliver_session_reply", direct_delivery)
    monkeypatch.setattr("app.services.openclaw_routing.enqueue_openclaw_message", direct_delivery)
    monkeypatch.setattr("app.services.openclaw_routing.wake_shared_lane", lambda *_args: None)
    return SimpleNamespace(
        agent=agent,
        sender=sender,
        session=session,
        row=row,
        at=at,
        tenant_id=tenant_id,
        calls=calls,
        direct_calls=direct_calls,
        inserted_id=inserted_id,
    )


def _assert_report_intents(store: _DeliveryStore, env: SimpleNamespace) -> None:
    assert set(store.intents) == {
        (env.row.id, "im", env.session.id),
        (env.row.id, "agent", env.sender.id),
    }
    assert all(intent.tenant_id == env.tenant_id for intent in store.intents.values())
    assert not env.direct_calls


async def test_delivery_store_enforces_per_message_destination_uniqueness(delivery_store: _DeliveryStore) -> None:
    message_id, destination_id, tenant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    kwargs = {
        "message_id": message_id,
        "destination_kind": "im",
        "destination_id": destination_id,
        "tenant_id": tenant_id,
    }

    async with delivery_store.connection_ctx():
        first = await delivery_store.create_intent(**kwargs)
        assert await delivery_store.create_intent(**kwargs) is None
        agent_intent = await delivery_store.create_intent(
            message_id=message_id,
            destination_kind="agent",
            destination_id=destination_id,
            tenant_id=tenant_id,
        )

    assert delivery_store.intents == {
        (message_id, "im", destination_id): first,
        (message_id, "agent", destination_id): agent_intent,
    }


async def test_report_first_result_wins_and_all_side_effects_run_once(
    monkeypatch: pytest.MonkeyPatch, delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env
    assert await gateway.report_result(GatewayReportRequest(message_id=env.row.id, result="first"), "key") == {
        "status": "ok"
    }
    monkeypatch.setattr(delivery, "_utcnow", lambda: env.at + timedelta(minutes=1))

    response = await gateway.report_result(GatewayReportRequest(message_id=env.row.id, result="second"), "key")

    assert response == {"status": "ok"}
    assert delivery_store.rows[env.row.id].result == "first"
    assert delivery_store.rows[env.row.id].completed_at == env.at
    assert all(len(calls) == 1 for calls in env.calls.values())
    assert env.calls["insert"][0]["content"] == "first"
    assert env.calls["insert"][0]["user_id"] == env.session.user_id
    assert env.calls["touch"][0]["obj_in"] == {"openclaw_last_seen": env.at}
    assert len(delivery_store.chat) == 1
    assert delivery_store.chat[0]["content"] == "first"
    _assert_report_intents(delivery_store, env)
    assert env.calls["ws"] == [
        (
            (
                str(env.agent.id),
                str(env.session.id),
                {
                    "type": "done",
                    "role": "assistant",
                    "content": "first",
                    "turn_id": str(env.row.id),
                    "message_id": str(env.inserted_id),
                    "entry_id": str(env.inserted_id),
                },
            ),
            {"user_id": str(env.session.user_id)},
        )
    ]


_CONFIGURED_REF = "openrouter/nvidia/nemotron-3.5-super-vl:free"


async def test_report_conceals_configured_model_ids_in_every_durable_and_live_destination(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env
    env.row.guest_model_ref = _CONFIGURED_REF

    await gateway.report_result(
        GatewayReportRequest(message_id=env.row.id, result=f"I am running on {_CONFIGURED_REF}."), "key"
    )

    expected = f"I am running on {get_settings().APP_NAME}."
    assert delivery_store.rows[env.row.id].result == expected  # the IM and agent relays read this stored result
    assert delivery_store.chat[0]["content"] == expected
    assert env.calls["ws"][0][0][2]["content"] == expected
    _assert_report_intents(delivery_store, env)


async def test_report_also_conceals_every_other_assigned_model(
    monkeypatch: pytest.MonkeyPatch, delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env
    fallback = LLMModelRecord(
        id=uuid.uuid4(),
        provider="openai",
        model="gpt-5.1-codex-max",
        api_key_encrypted="",
        label="Backup",
        tenant_id=env.tenant_id,
    )
    env.agent.fallback_model_id = fallback.id
    env.row.guest_model_ref = _CONFIGURED_REF

    async def get_many(ids: list[uuid.UUID]) -> list[LLMModelRecord]:
        return [fallback] if fallback.id in ids else []

    monkeypatch.setattr(llm_model_dao, "get_many", get_many)

    won = await delivery.record_report(env.agent, env.row.id, "Backup: gpt-5.1-codex-max (openai/gpt-5.1-codex-max).")

    brand = get_settings().APP_NAME
    assert won is not None
    assert won.message.result == f"Backup: {brand} ({brand})."


async def test_report_leaves_ordinary_model_discussion_unchanged(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env
    env.row.guest_model_ref = _CONFIGURED_REF
    text = "OpenRouter and NVIDIA publish many models; GPT-4o and Claude are common examples."

    won = await delivery.record_report(env.agent, env.row.id, text)

    assert won is not None
    assert won.message.result == delivery_store.rows[env.row.id].result == delivery_store.chat[0]["content"] == text


async def test_late_report_for_uncertain_delivery_wins_over_duplicate(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env
    env.row.status = "delivered"
    env.row.delivered_at = env.at - timedelta(days=1)

    await gateway.report_result(GatewayReportRequest(message_id=env.row.id, result="A"), "key")
    response = await gateway.report_result(GatewayReportRequest(message_id=env.row.id, result="B"), "key")

    assert response == {"status": "ok"}
    assert delivery_store.rows[env.row.id].result == "A"
    assert delivery_store.rows[env.row.id].status == "completed"
    assert all(len(calls) == 1 for calls in env.calls.values())
    assert len(delivery_store.chat) == 1
    assert delivery_store.chat[0]["content"] == "A"
    _assert_report_intents(delivery_store, env)


async def test_report_wins_while_requeued_row_is_still_pending(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env
    assert delivery_store.rows[env.row.id].status == "pending"

    await gateway.report_result(GatewayReportRequest(message_id=env.row.id, result="late"), "key")

    assert delivery_store.rows[env.row.id].status == "completed"
    assert delivery_store.rows[env.row.id].result == "late"
    assert all(len(calls) == 1 for calls in env.calls.values())
    assert len(delivery_store.chat) == 1
    assert delivery_store.chat[0]["content"] == "late"
    _assert_report_intents(delivery_store, env)


async def test_assistant_insert_failure_rolls_back_completion_and_never_publishes(
    monkeypatch: pytest.MonkeyPatch, delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    async def broken_insert(**_kwargs):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(delivery.chat_message_dao, "insert_message", broken_insert)

    with pytest.raises(RuntimeError, match="insert failed"):
        await gateway.report_result(GatewayReportRequest(message_id=report_env.row.id, result="answer"), "key")

    row = delivery_store.rows[report_env.row.id]
    assert row.status == "pending"
    assert row.result is None
    assert row.completed_at is None
    assert delivery_store.events == ["enter", "rollback"]
    assert all(not report_env.calls[name] for name in ("ws", "touch"))
    assert not delivery_store.chat
    assert not delivery_store.intents
    assert not report_env.direct_calls


async def test_failed_ws_publisher_preserves_durable_im_and_sender_intents(
    monkeypatch: pytest.MonkeyPatch, delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    async def broken_ws(*_args, **_kwargs):
        assert delivery_store.events[-1] == "commit"
        report_env.calls["ws"].append((_args, _kwargs))
        raise RuntimeError("disconnected")

    monkeypatch.setattr("app.api.websocket.manager", SimpleNamespace(send_to_session=broken_ws))

    response = await gateway.report_result(GatewayReportRequest(message_id=report_env.row.id, result="answer"), "key")

    assert response == {"status": "ok"}
    assert len(report_env.calls["ws"]) == 1
    assert len(delivery_store.chat) == 1
    assert delivery_store.chat[0]["content"] == "answer"
    _assert_report_intents(delivery_store, report_env)
    assert delivery_store.rows[report_env.row.id].status == "completed"


async def test_report_http_missing_key_unknown_id_and_duplicate(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    from app.main import app

    env = report_env
    env.row.status, env.row.result, env.row.completed_at = "completed", "original", env.at
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post("/api/gateway/report", json={"message_id": str(env.row.id), "result": "new"})
        unknown = await client.post(
            "/api/gateway/report",
            headers={"X-Api-Key": "key"},
            json={"message_id": str(uuid.uuid4()), "result": "new"},
        )
        duplicate = await client.post(
            "/api/gateway/report",
            headers={"X-Api-Key": "key"},
            json={"message_id": str(env.row.id), "result": "new"},
        )

    assert missing.status_code == 401
    assert missing.json() == {"detail": "Missing X-Api-Key header"}
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Message not found"}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "ok"}
    assert delivery_store.rows[env.row.id].result == "original"
    assert all(not calls for calls in env.calls.values())
    assert not delivery_store.chat
    assert not delivery_store.intents
    assert not env.direct_calls


async def test_second_intent_failure_rolls_back_chat_completion_and_first_intent(
    monkeypatch: pytest.MonkeyPatch, delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env
    attempted = []

    async def fail_second_intent(**kwargs):
        attempted.append(kwargs["destination_kind"])
        if len(attempted) == 2:
            assert len(delivery_store.intents) == len(delivery_store.chat) == 1
            assert delivery_store.rows[env.row.id].status == "completed"
            raise RuntimeError("second intent failed")
        return await delivery_store.create_intent(**kwargs)

    monkeypatch.setattr(delivery.gateway_report_delivery_dao, "create_intent", fail_second_intent)

    with pytest.raises(RuntimeError, match="second intent failed"):
        await gateway.report_result(GatewayReportRequest(message_id=env.row.id, result="answer"), "key")

    assert attempted == ["im", "agent"]
    row = delivery_store.rows[env.row.id]
    assert (row.status, row.result, row.completed_at) == ("pending", None, None)
    assert not delivery_store.chat
    assert not delivery_store.intents
    assert delivery_store.events == ["enter", "rollback"]
    assert not env.calls["ws"]
    assert not env.direct_calls


@pytest.mark.parametrize("destination", ["sender", "session", "both"])
async def test_report_excludes_foreign_tenant_sender_and_foreign_owned_session(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace, destination: str
) -> None:
    env = report_env
    if destination in ("sender", "both"):
        env.sender.tenant_id = uuid.uuid4()
    if destination in ("session", "both"):
        env.session.agent_id = uuid.uuid4()

    await gateway.report_result(GatewayReportRequest(message_id=env.row.id, result="answer"), "key")

    expected = set()
    if destination == "sender":
        expected.add((env.row.id, "im", env.session.id))
    if destination == "session":
        expected.add((env.row.id, "agent", env.sender.id))
    assert set(delivery_store.intents) == expected
    assert all(intent.tenant_id == env.tenant_id for intent in delivery_store.intents.values())
    assert not env.direct_calls


async def test_report_for_another_agents_message_is_not_found_and_writes_nothing(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env
    env.row.agent_id = uuid.uuid4()

    with pytest.raises(HTTPException) as caught:
        await gateway.report_result(GatewayReportRequest(message_id=env.row.id, result="answer"), "key")

    assert caught.value.status_code == 404
    assert (env.row.status, env.row.result, env.row.completed_at) == ("pending", None, None)
    assert not delivery_store.chat
    assert not delivery_store.intents
    assert all(not calls for calls in env.calls.values())
    assert not env.direct_calls


async def test_empty_report_keeps_delivery_intents_without_empty_assistant_chat(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env

    with pytest.raises(ValidationError):
        GatewayReportRequest(message_id=env.row.id, result="")
    won = await delivery.record_report(env.agent, env.row.id, "")
    assert won is not None
    assert won.assistant_message_id is None
    await delivery.publish_report(env.agent, won)
    assert await delivery.record_report(env.agent, env.row.id, "late") is None

    assert delivery_store.rows[env.row.id].result == ""
    assert delivery_store.rows[env.row.id].status == "completed"
    assert not delivery_store.chat
    assert not env.calls["insert"]
    assert len(env.calls["ws"]) == 1
    assert env.calls["ws"][0][0][2] == {"type": "done", "role": "assistant", "content": "", "turn_id": str(env.row.id)}
    _assert_report_intents(delivery_store, env)


async def test_publish_report_only_sends_authoritative_ws_without_creating_or_delivering_intents(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env
    delivery_store.events.append("commit")
    message = replace(env.row, status="completed", result="authoritative")

    await delivery.publish_report(env.agent, delivery.RecordedReport(message, None))

    assert env.calls["ws"] == [
        (
            (
                str(env.agent.id),
                str(env.session.id),
                {"type": "done", "role": "assistant", "content": "authoritative", "turn_id": str(env.row.id)},
            ),
            {"user_id": str(env.session.user_id)},
        )
    ]
    assert not delivery_store.intents
    assert not delivery_store.chat
    assert not env.direct_calls


async def test_record_report_retains_inserted_uuid_in_frozen_winning_result(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    env = report_env

    won = await delivery.record_report(env.agent, env.row.id, "answer")

    assert won is not None
    assert won.message.id == env.row.id
    assert won.message.result == "answer"
    assert won.assistant_message_id == env.inserted_id != env.row.id
    identity_field = "assistant_message_id"
    with pytest.raises(FrozenInstanceError):
        setattr(won, identity_field, uuid.uuid4())
    assert await delivery.record_report(env.agent, env.row.id, "late") is None
    assert len(delivery_store.chat) == 1
    assert not env.calls["ws"]


@pytest.mark.parametrize("conversation_id", [None, ""])
async def test_nonconversation_report_has_no_assistant_identity_or_notification(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace, conversation_id: str | None
) -> None:
    env = report_env
    env.row.conversation_id = conversation_id

    won = await delivery.record_report(env.agent, env.row.id, "answer")
    assert won is not None
    await delivery.publish_report(env.agent, won)

    assert won.assistant_message_id is None
    assert not delivery_store.chat
    assert not env.calls["ws"]


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_report_publishes_only_after_real_outer_transaction_commit(
    monkeypatch: pytest.MonkeyPatch, raw: _FakeRawConnection, report_env: SimpleNamespace, commit_fails: bool
) -> None:
    env = report_env
    monkeypatch.setattr(gateway, "connection_ctx", session_module.connection_ctx)

    async def commit():
        assert not env.calls["ws"]
        if commit_fails:
            raise RuntimeError("commit failed")
        raw.commits += 1

    async def ws(*args, **kwargs):
        assert raw.commits == 1
        assert session_module._conn_ctx.get() is None
        env.calls["ws"].append((args, kwargs))

    monkeypatch.setattr(raw, "commit", commit)
    monkeypatch.setattr("app.api.websocket.manager", SimpleNamespace(send_to_session=ws))
    request = GatewayReportRequest(message_id=env.row.id, result="answer")

    if commit_fails:
        with pytest.raises(RuntimeError, match="commit failed"):
            await gateway.report_result(request, "key")
        assert not env.calls["ws"]
    else:
        assert await gateway.report_result(request, "key") == {"status": "ok"}
        assert len(env.calls["ws"]) == 1


async def test_report_http_done_identity_matches_persisted_assistant(
    delivery_store: _DeliveryStore, report_env: SimpleNamespace
) -> None:
    from app.main import app

    env = report_env
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/gateway/report",
            headers={"X-Api-Key": "key"},
            json={"message_id": str(env.row.id), "result": "answer"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(delivery_store.chat) == len(env.calls["ws"]) == 1
    event = env.calls["ws"][0][0][2]
    assert event["content"] == delivery_store.chat[0]["content"] == "answer"
    assert event["message_id"] == event["entry_id"] == str(env.inserted_id)


API_KEY_CANARY = "gwk_canary_9f2e1d4c"


@pytest.fixture
def app_log() -> Iterator[list[str]]:
    chunks: list[str] = []
    configure_logging(level="DEBUG", fmt="text", enqueue=False, color=False, sink=chunks.append)
    yield chunks
    configure_logging()


def _app_output(chunks: list[str]) -> str:
    """Records from app modules only; the HTTP test client's own request log is not under test."""
    return "\n".join(line for line in "".join(chunks).splitlines() if " | app." in line)


async def test_report_diagnostics_identify_the_agent_never_the_api_key(
    report_env: SimpleNamespace, app_log: list[str]
) -> None:
    env = report_env

    reported = await gateway.report_result(GatewayReportRequest(message_id=env.row.id, result="answer"), API_KEY_CANARY)

    output = _app_output(app_log)
    assert reported == {"status": "ok"}
    assert f"[Gateway] report called agent={env.agent.id} msg_id={env.row.id}" in output
    assert API_KEY_CANARY[:8] not in output


async def test_rejected_api_key_is_unauthorized_and_absent_from_logs(
    monkeypatch: pytest.MonkeyPatch, app_log: list[str]
) -> None:
    from app.main import app

    monkeypatch.setattr(gateway.agent_dao, "get_openclaw_by_api_key", AsyncMock(return_value=None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/gateway/report",
            headers={"X-Api-Key": API_KEY_CANARY},
            json={"message_id": str(uuid.uuid4()), "result": "answer"},
        )

    output = _app_output(app_log)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid API key"}
    assert "<-- POST /api/gateway/report 401" in output
    assert API_KEY_CANARY[:8] not in output
