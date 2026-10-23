"""Work gates: typed rejection happens at each verified seam before any model, config or claim side effect."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request

from app.api import gateway
from app.main import app, openclaw_update_gate
from app.records.agent import AgentRecord
from app.services import openclaw_gateway_delivery as delivery, openclaw_routing as routing
from app.services.openclaw_update_gate import (
    OpenClawUpdateGateError,
    OpenClawUpdateInProgressError,
    OpenClawUpdateRequiredError,
)

NOTICE_ID = uuid.uuid4()


def _agent() -> AgentRecord:
    return AgentRecord(id=uuid.uuid4(), name="Ada", creator_id=uuid.uuid4(), tenant_id=uuid.uuid4())


def _closed(error: OpenClawUpdateGateError):
    async def gate(_agent_id: uuid.UUID) -> None:
        raise error

    return gate


@pytest.mark.parametrize("error", [OpenClawUpdateRequiredError(NOTICE_ID), OpenClawUpdateInProgressError()])
async def test_prepare_rejects_before_any_model_or_guest_config_side_effect(monkeypatch, error):
    ensure, bundle, config = AsyncMock(), AsyncMock(), Mock()
    monkeypatch.setattr(routing, "assert_work_allowed", _closed(error))
    monkeypatch.setattr(routing, "ensure_agent_company_models", ensure)
    monkeypatch.setattr(routing, "load_agent_model_bundle", bundle)
    monkeypatch.setattr(routing.agent_manager, "write_guest_config", config)
    with pytest.raises(OpenClawUpdateGateError) as raised:
        _ = await routing.prepare_openclaw_message(agent=_agent(), content="hello")
    assert raised.value is error
    ensure.assert_not_awaited()
    bundle.assert_not_awaited()
    config.assert_not_called()


async def test_poll_claim_rejects_before_requeue_or_claim(monkeypatch):
    dao = SimpleNamespace(claim_shared=AsyncMock(), requeue_stale_deliveries=AsyncMock(), claim_pending=AsyncMock())
    for name in ("claim_shared", "requeue_stale_deliveries", "claim_pending"):
        monkeypatch.setattr(delivery.gateway_message_dao, name, getattr(dao, name))
    monkeypatch.setattr(delivery, "assert_work_allowed", _closed(OpenClawUpdateRequiredError(NOTICE_ID)))
    with pytest.raises(OpenClawUpdateRequiredError):
        _ = await delivery.claim_poll_batch(uuid.uuid4())
    dao.claim_shared.assert_not_awaited()
    dao.requeue_stale_deliveries.assert_not_awaited()
    dao.claim_pending.assert_not_awaited()


async def test_background_wake_leaves_the_message_pending_and_unclaimed(monkeypatch):
    claims = SimpleNamespace(
        get_for_agent=AsyncMock(), claim_shared=AsyncMock(), mark_delivered=AsyncMock(), release_delivery=AsyncMock()
    )
    for name in ("get_for_agent", "claim_shared", "mark_delivered", "release_delivery"):
        monkeypatch.setattr(routing.gateway_message_dao, name, getattr(claims, name))
    wake = AsyncMock()
    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", wake)
    monkeypatch.setattr(routing, "assert_work_allowed", _closed(OpenClawUpdateInProgressError()))
    await routing._run_inbox_wake(_agent(), "content", uuid.uuid4())
    for mock in (claims.get_for_agent, claims.claim_shared, claims.mark_delivered, claims.release_delivery, wake):
        mock.assert_not_awaited()


async def test_guest_poll_stays_authenticated_and_touched_but_receives_no_work_while_gated(monkeypatch):
    agent = _agent()
    touched: list[uuid.UUID] = []

    async def authenticate(_key: str) -> AgentRecord:
        return agent

    async def touch(record: AgentRecord, *, touch_last_seen: bool = True) -> AgentRecord:
        touched.append(record.id)
        return record

    @asynccontextmanager
    async def transaction():
        yield None

    monkeypatch.setattr(gateway, "_get_agent_by_key", authenticate)
    monkeypatch.setattr(gateway.agent_dao, "touch_openclaw", touch)
    monkeypatch.setattr(gateway, "connection_ctx", transaction)
    monkeypatch.setattr(gateway, "claim_poll_batch", _closed_claim(OpenClawUpdateRequiredError(NOTICE_ID)))
    monkeypatch.setattr(gateway, "_poll_relationships", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.openclaw_hot_cache.should_touch_last_seen", lambda _id: True)
    response = await gateway.poll_messages("key")
    assert response.messages == []
    assert touched == [agent.id]


def _closed_claim(error: OpenClawUpdateGateError):
    async def claim(_agent_id: uuid.UUID):
        raise error

    return claim


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (
            OpenClawUpdateRequiredError(NOTICE_ID),
            423,
            {"code": "openclaw_update_required", "notice_id": str(NOTICE_ID)},
        ),
        (OpenClawUpdateInProgressError(), 409, {"code": "openclaw_update_in_progress"}),
    ],
)
async def test_http_maps_typed_gate_errors_to_423_and_409(error, status, detail):
    assert app.exception_handlers[OpenClawUpdateGateError] is openclaw_update_gate
    response = await openclaw_update_gate(Request({"type": "http"}), error)
    assert response.status_code == status
    assert json.loads(bytes(response.body)) == {"detail": detail}


def test_chat_socket_rejection_carries_the_code_and_only_required_carries_the_notice():
    required = OpenClawUpdateRequiredError(NOTICE_ID).rejection()
    busy = OpenClawUpdateInProgressError().rejection()
    assert required["reason"] == "openclaw_update_required"
    assert required["notice_id"] == str(NOTICE_ID)
    assert busy["reason"] == "openclaw_update_in_progress"
    assert "notice_id" not in busy
    assert required["content"]
    assert busy["content"]
