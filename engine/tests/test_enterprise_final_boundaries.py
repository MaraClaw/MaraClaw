import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from tests.test_enterprise_transaction_boundaries import _actor, _model, owned_pool as owned_pool

from app.api import enterprise
from app.dao.audit_log_dao import audit_log_dao
from app.dao.user_dao import user_dao
from app.db import session
from app.records.agent import AgentRecord
from app.records.audit import ApprovalRequestRecord
from app.records.user import UserRecord
from app.services import autonomy_service as autonomy_module, notification_service
from app.services.subscription_boundaries import fresh_subscription_actor


@pytest.mark.parametrize("reload", [1, 2])
@pytest.mark.parametrize("change", ["identity_id", "active", "tenant"])
async def test_actor_rejects_changed_membership_on_either_reload(monkeypatch, owned_pool, reload, change):
    user = _actor(_model())
    changed = replace(user, identity_id=uuid.uuid4())
    if change == "active":
        changed = replace(user, is_active=False)
    elif change == "tenant":
        changed = replace(user, tenant_id=uuid.uuid4())
    monkeypatch.setattr(
        user_dao, "get_with_identity", AsyncMock(side_effect=[changed, changed] if reload == 1 else [user, changed])
    )

    with pytest.raises(HTTPException) as error:
        async with session.connection_ctx():
            await fresh_subscription_actor(user)

    assert error.value.status_code == 403
    assert owned_pool.raw.commits == 0


@pytest.mark.parametrize("outcome", ["success", "audit_error", "cancel", "commit_error"])
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_approval_audit_is_atomic_and_remote_work_follows_commit(monkeypatch, owned_pool, outcome, action):
    user = _actor(_model())
    agent = AgentRecord(id=uuid.uuid4(), name="Agent", creator_id=user.id)
    pending = ApprovalRequestRecord(
        id=uuid.uuid4(), agent_id=agent.id, action_type="tool", details={"tool": "test"}, created_at=datetime.now(UTC)
    )
    status = "approved" if action == "approve" else "rejected"
    resolved = replace(pending, status=status, resolved_by=user.id)
    observed = []
    effects = []

    async def resolve(*args, **kwargs):
        observed.append(session.get_connection())
        session.run_after_commit(lambda: effects.append("resolved"))
        return resolved

    async def audit(**kwargs):
        observed.append(session.get_connection())
        if outcome == "audit_error":
            raise RuntimeError("audit failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        session.run_after_commit(lambda: effects.append("audited"))

    async def execute(*args):
        assert session.get_connection() is None
        assert owned_pool.active == 0
        assert effects == ["resolved", "audited"]
        async with session.connection_ctx():
            pass
        return "executed"

    async def notify(**kwargs):
        assert session.get_connection() is None
        assert owned_pool.active == 0
        assert effects == ["resolved", "audited"]

    service = autonomy_module.AutonomyService()
    monkeypatch.setattr(autonomy_module.approval_request_dao, "get", AsyncMock(return_value=pending))
    monkeypatch.setattr(autonomy_module.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(autonomy_module.approval_request_dao, "resolve", resolve)
    monkeypatch.setattr(audit_log_dao, "create", audit)
    execution = AsyncMock(side_effect=execute)
    notification = AsyncMock(side_effect=notify)
    monkeypatch.setattr(service, "_execute_approved_action", execution)
    monkeypatch.setattr(notification_service, "send_notification", notification)
    if outcome == "commit_error":
        monkeypatch.setattr(owned_pool.raw, "commit", AsyncMock(side_effect=RuntimeError("commit failed")))

    if outcome == "success":
        app = FastAPI()
        app.include_router(enterprise.router)
        app.dependency_overrides[enterprise.get_current_user] = lambda: user
        monkeypatch.setattr(enterprise, "autonomy_service", service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/enterprise/approvals/{pending.id}/resolve", json={"action": action})
            assert response.status_code == 200
            assert response.json()["status"] == status
        if action == "approve":
            execution.assert_awaited_once()
        else:
            execution.assert_not_awaited()
        notification.assert_awaited_once()
    else:
        error = asyncio.CancelledError if outcome == "cancel" else RuntimeError
        with pytest.raises(error):
            await service.resolve_approval(pending.id, user, action)
        execution.assert_not_awaited()
        notification.assert_not_awaited()
        assert effects == []
        assert owned_pool.raw.rollbacks == 1
    assert observed[0] is not None
    assert observed[0] is observed[1]


@pytest.mark.parametrize("tenant_id", [None, ""])
async def test_tenantless_org_admin_cannot_read_unscoped_audit_logs(monkeypatch, tenant_id):
    user = UserRecord(id=uuid.uuid4(), role="org_admin")
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(audit_log_dao, "list_scoped", listing)

    with pytest.raises(HTTPException) as error:
        await enterprise.list_audit_logs(tenant_id=tenant_id, current_user=user)

    assert error.value.status_code == 403
    listing.assert_not_awaited()
