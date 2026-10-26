import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import test_agent_setup_lifecycle as setup
from app.dao.agent_setup_mcp_dao import agent_setup_mcp_dao
from app.records.agent_setup_mcp import MCPWarningCode
from app.services import agent_setup, agent_setup_mcp
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError

setup_db = setup.setup_db
lifecycle = setup.lifecycle
mcp_http = setup.mcp_http


async def warning(lifecycle, code: MCPWarningCode = "connection_failed"):
    _db, actor, tenant, _foreign, _identity = lifecycle
    agent = await setup.provision(actor, tenant, servers=("vendor/qa",))
    await setup.finish_required(agent)
    claim = await agent_setup_mcp_dao.claim()
    assert claim is not None
    assert await agent_setup_mcp_dao.finish(claim, code)
    return agent


@pytest.mark.parametrize("gate", ["unsupported", "expired", "owner_changed", "tenant_changed"])
async def test_exact_retry_refuses_nonretryable_or_changed_ownership(lifecycle, mcp_http, gate):
    db, _actor, _tenant, foreign, _identity = lifecycle
    agent = await warning(lifecycle, "unsupported_remote_transport" if gate == "unsupported" else "connection_failed")
    match gate:
        case "expired":
            await db.execute("UPDATE agents SET expires_at=clock_timestamp() WHERE id=%(id)s", {"id": agent.id})
        case "owner_changed":
            owner = uuid4()
            await db.execute("INSERT INTO users (id,display_name) VALUES (%(id)s,'Owner')", {"id": owner})
            await db.execute("UPDATE agents SET creator_id=%(owner)s WHERE id=%(id)s", {"owner": owner, "id": agent.id})
        case "tenant_changed":
            await db.execute(
                "UPDATE agents SET tenant_id=%(tenant)s WHERE id=%(id)s", {"tenant": foreign, "id": agent.id}
            )
            await db.execute("UPDATE users SET tenant_id=%(tenant)s WHERE id=%(id)s", {"tenant": foreign, "id": _actor})
    response = await mcp_http.post(f"/api/agents/{agent.id}/setup/mcp-retry", json={"server_id": "vendor/qa"})
    assert response.status_code == 409
    expected = "agent_setup_mcp_not_retryable" if gate == "unsupported" else "agent_setup_mcp_ineligible"
    assert response.json()["detail"] == {"code": expected}
    row = await agent_setup_mcp_dao.get(agent.id, "vendor/qa")
    assert row is not None
    assert row.state == "warning"


@pytest.mark.parametrize("gate", ["user_disabled", "identity_disabled", "upgrade"])
async def test_retry_rechecks_authorization_and_upgrade_after_flock(lifecycle, mcp_http, monkeypatch, gate):
    db, actor, _tenant, _foreign, identity = lifecycle
    agent = await warning(lifecycle)
    reached = asyncio.Event()
    original = agent_setup._setup_manager

    async def authorize(user_id, agent_id):
        result = await original(user_id, agent_id)
        reached.set()
        return result

    monkeypatch.setattr(agent_setup, "_setup_manager", authorize)
    async with setup_workspace_lock(agent_setup.agent_manager._agent_dir(agent.id)):
        retry = asyncio.create_task(
            mcp_http.post(f"/api/agents/{agent.id}/setup/mcp-retry", json={"server_id": "vendor/qa"})
        )
        await asyncio.wait_for(reached.wait(), 5)
        match gate:
            case "user_disabled":
                await db.execute("UPDATE users SET is_active=false WHERE id=%(id)s", {"id": actor})
            case "identity_disabled":
                await db.execute("UPDATE identities SET is_active=false WHERE id=%(id)s", {"id": identity})
            case "upgrade":
                monkeypatch.setattr(
                    agent_setup_mcp, "assert_no_active_run", AsyncMock(side_effect=OpenClawUpdateInProgressError())
                )
    if gate == "upgrade":
        with pytest.raises(OpenClawUpdateInProgressError):
            await asyncio.wait_for(retry, 5)
    else:
        assert (await asyncio.wait_for(retry, 5)).status_code == 401
    row = await agent_setup_mcp_dao.get(agent.id, "vendor/qa")
    assert row is not None
    assert row.state == "warning"


async def test_use_only_principal_cannot_retry_optional_work(lifecycle, mcp_http):
    db, actor, _tenant, _foreign, _identity = lifecycle
    agent = await warning(lifecycle)
    await db.execute("UPDATE users SET role='member' WHERE id=%(id)s", {"id": actor})
    owner = uuid4()
    await db.execute("INSERT INTO users (id,display_name) VALUES (%(id)s,'Owner')", {"id": owner})
    await db.execute(
        "UPDATE agents SET creator_id=%(owner)s,access_mode='company',company_access_level='use' WHERE id=%(id)s",
        {"owner": owner, "id": agent.id},
    )
    response = await mcp_http.post(f"/api/agents/{agent.id}/setup/mcp-retry", json={"server_id": "vendor/qa"})
    assert response.status_code == 403
    assert response.json()["detail"] == {"code": "agent_setup_manage_required"}
