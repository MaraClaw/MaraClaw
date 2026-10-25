import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import test_agent_setup_retry_postgres
from app.dao import agent_setup_dao
from app.services import agent_setup, openclaw_update_exclusion
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError
from test_agent_setup_retry_postgres import failed_setup

setup_db = test_agent_setup_retry_postgres.setup_db
setup_http = test_agent_setup_retry_postgres.setup_http


@pytest.mark.parametrize("method", ["get", "post"])
async def test_use_access_does_not_grant_setup_management(setup_db, setup_http, method):
    # Given a company-visible employee that the caller can use but not manage.
    db, _actor = setup_db
    owner = uuid4()
    await db.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'Owner')", {"id": owner})
    agent_id = await failed_setup(owner)
    await db.execute(
        "UPDATE agents SET access_mode = 'company', company_access_level = 'use' WHERE id = %(id)s", {"id": agent_id}
    )
    # When the use-only member requests recovery metadata or retry.
    suffix = "/retry" if method == "post" else ""
    response = await setup_http.request(method, f"/api/agents/{agent_id}/setup{suffix}")
    # Then manage authorization is required, independently of ordinary use access.
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "agent_setup_manage_required"


@pytest.mark.parametrize("gate", ["disabled_identity", "force_password", "disabled_tenant"])
async def test_retry_reloads_current_identity_and_tenant_gates(setup_db, setup_http, gate):
    # Given a dependency snapshot predating a current principal or tenant restriction.
    db, actor = setup_db
    agent_id = await failed_setup(actor)
    identity_id = uuid4()
    await db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": identity_id})
    await db.execute(
        "UPDATE users SET identity_id = %(identity)s WHERE id = %(id)s", {"identity": identity_id, "id": actor}
    )
    match gate:
        case "disabled_identity":
            await db.execute("UPDATE identities SET is_active = false WHERE id = %(id)s", {"id": identity_id})
        case "force_password":
            await db.execute("UPDATE identities SET must_change_password = true WHERE id = %(id)s", {"id": identity_id})
        case "disabled_tenant":
            tenant_id = uuid4()
            await db.execute(
                "INSERT INTO tenants (id, name, slug, is_active) VALUES (%(id)s, 'Disabled', 'disabled', false)",
                {"id": tenant_id},
            )
            await db.execute(
                "UPDATE users SET tenant_id = %(tenant)s WHERE id = %(id)s", {"id": actor, "tenant": tenant_id}
            )
            await db.execute(
                "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(id)s", {"id": agent_id, "tenant": tenant_id}
            )
    # When explicit retry is requested.
    response = await setup_http.post(f"/api/agents/{agent_id}/setup/retry")
    # Then fresh security state rejects it without altering the failure.
    assert response.status_code == (401 if gate == "disabled_identity" else 403)
    row = await agent_setup_dao.get(agent_id)
    assert row is not None
    assert row.state == "failed"


async def test_retry_reauthorizes_after_waiting_for_workspace_lock(setup_db, setup_http, monkeypatch):
    # Given a retry blocked behind another workspace writer.
    db, actor = setup_db
    agent_id = await failed_setup(actor)
    reached_lock = asyncio.Event()
    original = agent_setup._setup_manager

    async def authorize(user_id, target_id):
        result = await original(user_id, target_id)
        reached_lock.set()
        return result

    monkeypatch.setattr(agent_setup, "_setup_manager", authorize)
    async with setup_workspace_lock(agent_setup.agent_manager._agent_dir(agent_id)):
        retry = asyncio.create_task(setup_http.post(f"/api/agents/{agent_id}/setup/retry"))
        await asyncio.wait_for(reached_lock.wait(), 5)
        await db.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": actor})
    # When the lock becomes available after deactivation.
    response = await asyncio.wait_for(retry, 5)
    # Then the pre-lock authorization does not authorize a stale principal.
    assert response.status_code == 401


async def test_successful_retry_does_not_hide_new_runtime_error(setup_db, setup_http):
    # Given a replay whose agent acquired a distinct runtime error after retry acceptance.
    db, actor = setup_db
    agent_id = await failed_setup(actor)
    assert (await setup_http.post(f"/api/agents/{agent_id}/setup/retry")).status_code == 202
    claim = await agent_setup_dao.claim()
    assert claim is not None
    await db.execute("UPDATE agents SET status = 'error', container_id = 'runtime' WHERE id = %(id)s", {"id": agent_id})
    # When setup itself succeeds.
    assert await agent_setup_dao.finish(claim)
    # Then only setup_pending clears; the runtime error remains observable.
    row = await db.fetchone("SELECT setup_pending, status FROM agents WHERE id = %(id)s", {"id": agent_id})
    assert row == {"setup_pending": False, "status": "error"}


async def test_retry_rechecks_upgrade_exclusion_under_workspace_lock(setup_db, monkeypatch):
    # Given an upgrade that appears between the pre-lock and in-lock checks.
    _db, actor = setup_db
    agent_id = await failed_setup(actor)
    gate = AsyncMock(side_effect=[None, OpenClawUpdateInProgressError()])
    monkeypatch.setattr(openclaw_update_exclusion, "assert_no_active_run", gate)
    # When retry reaches the exclusion boundary.
    with pytest.raises(OpenClawUpdateInProgressError):
        await agent_setup.retry_agent_setup(agent_id, actor)
    # Then active upgrade ownership wins and failed work is not requeued.
    assert gate.await_count == 2
    row = await agent_setup_dao.get(agent_id)
    assert row is not None
    assert row.state == "failed"
