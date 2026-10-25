import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import test_agent_setup_postgres
from app.dao import agent_dao, agent_setup_dao, user_dao
from app.services import agent_setup
from test_agent_setup_postgres import create_pending

setup_db = test_agent_setup_postgres.setup_db


@pytest.fixture
async def setup_http(setup_db):
    import app.api.agent_setup as api

    _db, actor_id = setup_db
    actor = await user_dao.get(actor_id)
    assert actor is not None
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: actor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://setup-qa") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def failed_setup(actor: UUID) -> UUID:
    agent_id = await create_pending(actor)
    claim = await agent_setup_dao.claim()
    assert claim is not None
    assert await agent_setup_dao.finish(claim, "setup_failed")
    return agent_id


async def test_http_status_is_safe_and_failed_retry_is_observable(setup_db, setup_http):
    # Given failed durable setup.
    _db, actor = setup_db
    agent_id = await failed_setup(actor)
    # When the manager observes the failure.
    response = await setup_http.get(f"/api/agents/{agent_id}/setup")
    # Then only the safe recovery contract is disclosed.
    assert response.status_code == 200
    assert response.json() == {
        "setup_pending": True,
        "state": "failed",
        "failure_code": "setup_failed",
        "retryable": True,
    }


async def test_http_concurrent_retries_enqueue_once_and_keep_start_blocked(setup_db, setup_http):
    # Given a terminal failure.
    _db, actor = setup_db
    agent_id = await failed_setup(actor)
    # When two authorized retry requests race.
    responses = await asyncio.gather(*[setup_http.post(f"/api/agents/{agent_id}/setup/retry") for _ in range(2)])
    # Then exactly one failed->pending transition wins.
    assert sorted(response.status_code for response in responses) == [202, 409]
    accepted = next(response for response in responses if response.status_code == 202)
    assert accepted.json() == {"setup_pending": True, "state": "pending", "failure_code": None, "retryable": False}
    agent = await agent_dao.get(agent_id)
    assert agent is not None
    assert agent.setup_pending
    with pytest.raises(ValueError, match="setup is incomplete"):
        await agent_setup.agent_manager.start_container(agent)


async def test_successful_retry_restores_idle_and_discards_sensitive_inputs(setup_db, setup_http):
    # Given a failed setup explicitly requeued by its manager.
    _db, actor = setup_db
    agent_id = await failed_setup(actor)
    assert (await setup_http.post(f"/api/agents/{agent_id}/setup/retry")).status_code == 202
    claim = await agent_setup_dao.claim()
    assert claim is not None
    # When the worker successfully replays it.
    await agent_setup.run_agent_setup(claim)
    # Then success is final, the agent is idle, and setup credentials are cleared.
    status = await setup_http.get(f"/api/agents/{agent_id}/setup")
    assert status.json() == {"setup_pending": False, "state": "succeeded", "failure_code": None, "retryable": False}
    agent = await agent_dao.get(agent_id)
    assert agent is not None
    assert agent.status == "idle"
    record = await agent_setup_dao.get(agent_id)
    assert record is not None
    assert not record.inputs.gateway_key_encrypted
    assert not record.inputs.files
    assert (await setup_http.post(f"/api/agents/{agent_id}/setup/retry")).status_code == 409


@pytest.mark.parametrize("expired", [False, True])
async def test_retry_cannot_steal_running_lease_even_when_expired(setup_db, setup_http, expired):
    # Given a worker-owned row, possibly awaiting crash recovery.
    db, actor = setup_db
    agent_id = await create_pending(actor)
    claim = await agent_setup_dao.claim()
    assert claim is not None
    if expired:
        await db.execute(
            "UPDATE agent_setups SET lease_until = clock_timestamp() WHERE agent_id = %(id)s", {"id": agent_id}
        )
    # When a manager attempts explicit retry.
    response = await setup_http.post(f"/api/agents/{agent_id}/setup/retry")
    # Then neither ownership nor its token is reset.
    assert response.status_code == 409
    row = await agent_setup_dao.get(agent_id)
    assert row is not None
    assert row.state == "running"
    assert row.claim_token == claim.claim_token


async def test_old_claim_cannot_publish_after_explicit_retry(setup_db, setup_http):
    # Given a failed first owner and an authorized new attempt.
    _db, actor = setup_db
    agent_id = await create_pending(actor)
    old = await agent_setup_dao.claim()
    assert old is not None
    assert await agent_setup_dao.finish(old, "setup_failed")
    assert (await setup_http.post(f"/api/agents/{agent_id}/setup/retry")).status_code == 202
    current = await agent_setup_dao.claim()
    assert current is not None
    assert current.claim_token != old.claim_token
    # When the old owner attempts late completion.
    assert not await agent_setup_dao.finish(old)
    # Then the new lease remains live.
    assert await agent_setup_dao.owns(current)


@pytest.mark.parametrize("method", ["get", "post"])
async def test_nonmanager_cannot_observe_or_retry_setup(setup_db, setup_http, method):
    # Given an agent owned by another active member.
    db, _actor = setup_db
    owner = uuid4()
    await db.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'Other')", {"id": owner})
    agent_id = await failed_setup(owner)
    suffix = "/retry" if method == "post" else ""
    # When the unrelated member requests status/retry.
    response = await setup_http.request(method, f"/api/agents/{agent_id}/setup{suffix}")
    # Then recovery metadata is manage-only.
    assert response.status_code == 403


async def test_retry_rechecks_disabled_membership_instead_of_dependency_snapshot(setup_db, setup_http):
    # Given a cached active principal whose membership has since been disabled.
    db, actor = setup_db
    agent_id = await failed_setup(actor)
    await db.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": actor})
    # When it attempts retry with the old dependency snapshot.
    response = await setup_http.post(f"/api/agents/{agent_id}/setup/retry")
    # Then current principal state wins.
    assert response.status_code == 401


async def test_no_setup_row_has_explicit_empty_status(setup_db, setup_http):
    # Given a legacy already-created agent without durable work.
    _db, actor = setup_db
    agent = await agent_dao.create(obj_in={"name": "Legacy", "creator_id": actor})
    # When its manager reads setup status.
    response = await setup_http.get(f"/api/agents/{agent.id}/setup")
    # Then absence is typed rather than a retry invitation.
    assert response.json() == {"setup_pending": False, "state": None, "failure_code": None, "retryable": False}


@pytest.mark.parametrize("ineligible", ["pending", "completed_agent", "missing_credentials"])
async def test_retry_rejects_ineligible_work_without_mutation(setup_db, setup_http, ineligible):
    # Given work that is not both failed and eligible for durable replay.
    db, actor = setup_db
    agent_id = await create_pending(actor) if ineligible == "pending" else await failed_setup(actor)
    match ineligible:
        case "completed_agent":
            await db.execute("UPDATE agents SET setup_pending = false WHERE id = %(id)s", {"id": agent_id})
        case "missing_credentials":
            await db.execute(
                'UPDATE agent_setups SET inputs = \'{"gateway_key_encrypted":""}\' WHERE agent_id = %(id)s',
                {"id": agent_id},
            )
    # When a manager explicitly retries it.
    response = await setup_http.post(f"/api/agents/{agent_id}/setup/retry")
    # Then no new pending attempt is accepted.
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "agent_setup_not_retryable"
    status = await setup_http.get(f"/api/agents/{agent_id}/setup")
    assert status.json()["retryable"] is False
