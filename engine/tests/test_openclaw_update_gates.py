"""Work gates: typed rejection happens at each verified seam before any model, config or claim side effect."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request

from app.main import app, openclaw_update_gate
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_native_claims as claims, openclaw_routing as routing
from app.services.agent_manager import agent_manager
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
    monkeypatch.setattr(agent_manager, "write_guest_config", config)
    with pytest.raises(OpenClawUpdateGateError) as raised:
        _ = await routing.prepare_openclaw_message(agent=_agent(), content="hello")
    assert raised.value is error
    ensure.assert_not_awaited()
    bundle.assert_not_awaited()
    config.assert_not_called()


@pytest.mark.parametrize("error", [OpenClawUpdateRequiredError(NOTICE_ID), OpenClawUpdateInProgressError()])
async def test_background_wake_leaves_the_message_pending_and_unclaimed(
    monkeypatch: pytest.MonkeyPatch, error: OpenClawUpdateGateError
) -> None:
    agent = _agent()
    row = GatewayMessageRecord(id=uuid.uuid4(), agent_id=agent.id, content="content")
    claim, release = AsyncMock(), AsyncMock()
    rolled_back = False

    @asynccontextmanager
    async def transaction() -> AsyncGenerator[None]:
        nonlocal rolled_back
        try:
            yield None
        except OpenClawUpdateGateError:
            rolled_back = True
            raise

    @asynccontextmanager
    async def guest(_agent_id: uuid.UUID) -> AsyncGenerator[None]:
        yield None

    monkeypatch.setattr(claims, "connection_ctx", transaction)
    monkeypatch.setattr(claims, "guest_write_section", guest)
    for name in ("lock_tenants", "lock_user", "lock_agent"):
        monkeypatch.setattr(claims, name, AsyncMock())
    monkeypatch.setattr(claims, "get_connection", lambda: None)
    monkeypatch.setattr(claims, "assert_work_allowed", _closed(error))
    monkeypatch.setattr(routing.gateway_message_dao, "get_for_agent", AsyncMock(return_value=row))
    monkeypatch.setattr(claims.gateway_message_dao, "claim_ordinary_head", claim)
    monkeypatch.setattr(claims.gateway_message_dao, "release_delivery", release)
    await routing._run_inbox_wake(agent, row.content, row.id)
    assert rolled_back
    claim.assert_not_awaited()
    release.assert_not_awaited()


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
