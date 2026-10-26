import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import test_agent_setup_lifecycle as setup
from app.dao.agent_setup_mcp_dao import agent_setup_mcp_dao
from app.services import agent_setup_mcp_worker as worker
from app.services.mcp_installation import MCPInstallation
from app.services.smithery_import import MCPImportOutcome

setup_db = setup.setup_db
lifecycle = setup.lifecycle


async def ready_claim(lifecycle):
    _db, actor, tenant, _foreign, _identity = lifecycle
    agent = await setup.provision(actor, tenant, servers=("vendor/qa",))
    await setup.finish_required(agent)
    claim = await agent_setup_mcp_dao.claim()
    assert claim is not None
    return agent, claim


async def restrict(lifecycle, agent_id, gate):
    db, actor, tenant, foreign, identity = lifecycle
    params = {"agent": agent_id, "actor": actor, "tenant": tenant, "foreign": foreign, "identity": identity}
    match gate:
        case "owner_disabled":
            await db.execute("UPDATE users SET is_active=false WHERE id=%(actor)s", params)
        case "identity_disabled":
            await db.execute("UPDATE identities SET is_active=false WHERE id=%(identity)s", params)
        case "tenant_disabled":
            await db.execute("UPDATE tenants SET is_active=false WHERE id=%(tenant)s", params)
        case "expired":
            await db.execute("UPDATE agents SET expires_at=clock_timestamp() WHERE id=%(agent)s", params)
        case "owner_changed":
            owner = uuid4()
            await db.execute("INSERT INTO users (id,display_name) VALUES (%(id)s,'New owner')", {"id": owner})
            await db.execute("UPDATE agents SET creator_id=%(owner)s WHERE id=%(agent)s", {**params, "owner": owner})
        case "tenant_changed":
            await db.execute("UPDATE agents SET tenant_id=%(foreign)s WHERE id=%(agent)s", params)
        case "lease_expired":
            await db.execute(
                "UPDATE agent_setup_mcp_work SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE agent_id=%(agent)s",
                params,
            )
        case "deleted":
            await db.execute("DELETE FROM agents WHERE id=%(agent)s", params)
        case "upgrade":
            notice = uuid4()
            await db.execute(
                "INSERT INTO openclaw_update_notices (id,request_id,title,body,mandatory) "
                "VALUES (%(id)s,%(id)s,'QA','QA',false)",
                {"id": notice},
            )
            await db.execute(
                "INSERT INTO openclaw_update_runs (id,notice_id,agent_id,state) VALUES (%(id)s,%(notice)s,%(agent)s,'pending')",
                {"id": uuid4(), "notice": notice, "agent": agent_id},
            )


@pytest.mark.parametrize("phase", ["before_import", "before_publication"])
@pytest.mark.parametrize(
    "gate",
    [
        "owner_disabled",
        "identity_disabled",
        "tenant_disabled",
        "expired",
        "owner_changed",
        "tenant_changed",
        "lease_expired",
        "deleted",
        "upgrade",
    ],
)
async def test_ineligible_or_stale_claim_cannot_import_or_publish(lifecycle, monkeypatch, phase, gate):
    agent, claim = await ready_claim(lifecycle)
    publish = AsyncMock()

    async def prepare(_server, _agent):
        await restrict(lifecycle, agent.id, gate)
        return MCPImportOutcome(None, "")

    prepare_spy = AsyncMock(side_effect=prepare if phase == "before_publication" else None)
    monkeypatch.setattr(worker, "prepare_smithery_import", prepare_spy)
    monkeypatch.setattr(worker, "publish_smithery_import", publish)
    if phase == "before_import":
        await restrict(lifecycle, agent.id, gate)
    await worker.run_agent_setup_mcp(claim)
    assert prepare_spy.await_count == (1 if phase == "before_publication" else 0)
    publish.assert_not_awaited()
    row = await agent_setup_mcp_dao.get(agent.id, claim.server_id)
    assert row is None or row.state != "succeeded"
    assert not await agent_setup_mcp_dao.finish(claim, None)


async def test_competing_claims_and_crash_recovery_fence_previous_owner(lifecycle):
    agent, old = await ready_claim(lifecycle)
    assert await agent_setup_mcp_dao.claim() is None
    assert await agent_setup_mcp_dao.renew(old)
    await restrict(lifecycle, agent.id, "lease_expired")
    claims = await asyncio.gather(agent_setup_mcp_dao.claim(), agent_setup_mcp_dao.claim())
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].claim_token != old.claim_token
    assert winners[0].attempts == 2
    assert not await agent_setup_mcp_dao.renew(old)
    assert not await agent_setup_mcp_dao.finish(old, None)
    await agent_setup_mcp_dao.release(old)
    assert await agent_setup_mcp_dao.owns(winners[0])


async def test_publication_and_completion_rollback_together_when_claim_lost(lifecycle, monkeypatch):
    agent, claim = await ready_claim(lifecycle)
    outcome = MCPImportOutcome(
        None,
        "",
        MCPInstallation("mcp_qa", "QA", "https://qa.invalid", "QA", {}),
        ({"name": "ping", "description": "", "parameters_schema": {"type": "object", "properties": {}}},),
    )
    monkeypatch.setattr(worker, "prepare_smithery_import", AsyncMock(return_value=outcome))
    monkeypatch.setattr(agent_setup_mcp_dao, "finish", AsyncMock(return_value=False))
    await worker.run_agent_setup_mcp(claim)
    db, *_rest = lifecycle
    assert await db.fetchval("SELECT count(*) FROM tools WHERE name='mcp_qa_ping'") == 0
    assert await db.fetchval("SELECT count(*) FROM agent_tools WHERE agent_id=%(id)s", {"id": agent.id}) == 0


async def test_cancellation_drains_import_and_releases_workspace_lock(lifecycle, monkeypatch):
    agent, claim = await ready_claim(lifecycle)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def prepare(_server, _agent):
        entered.set()
        await resume.wait()
        return MCPImportOutcome(None, "")

    monkeypatch.setattr(worker, "prepare_smithery_import", prepare)
    task = asyncio.create_task(worker.run_agent_setup_mcp(claim))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    assert not task.done()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    async with worker.setup_workspace_lock(worker.agent_manager._agent_dir(agent.id), timeout_seconds=1):
        row = await agent_setup_mcp_dao.get(agent.id, claim.server_id)
        assert row is not None
        assert row.state == "succeeded"
