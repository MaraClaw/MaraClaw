import uuid
from typing import Literal, assert_never
from unittest.mock import AsyncMock

import anyio
import pytest

from app.api import onboarding as onboarding_api
from app.core.domain.agents import AgentStatus
from app.records.agent import AgentRecord
from app.records.onboarding import UserTenantOnboardingRecord
from app.records.tenant import TenantRecord
from app.services.agent_lifecycle import LifecycleFailure
from app.services.guest_admission import GuestStartPreflightError
from test_agent_create_departments import creation
from test_onboarding import assistant_creation

__all__ = ["assistant_creation", "creation"]


async def test_new_assistant_uses_shared_defaults_private_grants_and_no_pending_setup(assistant_creation, monkeypatch):
    fixture, client = assistant_creation
    tenant_id = fixture.store.actor.tenant_id
    assert tenant_id is not None
    tenant = TenantRecord(
        id=tenant_id,
        name="Company",
        slug="company",
        default_agent_ttl_hours=17,
        default_max_llm_calls_per_day=31,
        default_max_triggers=7,
        min_poll_interval_floor=11,
        max_webhook_rate_ceiling=13,
        min_heartbeat_interval_minutes=360,
    )
    monkeypatch.setattr(onboarding_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    before = onboarding_api.datetime.now(onboarding_api.UTC)
    response = await client.post("/api/onboarding/personal-assistant", json={"name": " Private "})
    assert response.status_code == 200
    assert response.json()["start"] == {"status": "started"}
    agent = fixture.store.agent_rows[0]
    assert not agent.setup_pending
    assert agent.status == "running"
    assert agent.expires_at is not None
    assert 16.99 < (agent.expires_at - before).total_seconds() / 3600 < 17.01
    assert (
        agent.max_llm_calls_per_day,
        agent.max_triggers,
        agent.min_poll_interval_min,
        agent.webhook_rate_limit,
        agent.heartbeat_interval_minutes,
    ) == (31, 7, 11, 13, 360)
    assert agent.access_mode == "private"
    assert [(grant.scope_type, grant.scope_id, grant.access_level) for grant in fixture.store.grants] == [
        ("user", fixture.store.actor.id, "manage")
    ]
    assert fixture.rows[0].personal_assistant_agent_id == agent.id
    fixture.store.setup.assert_not_awaited()


@pytest.mark.parametrize("failure", ["preflight", "lifecycle", "initialization"])
async def test_failed_start_preserves_retryable_assistant_and_returns_typed_outcome(
    assistant_creation, monkeypatch, failure: Literal["preflight", "lifecycle", "initialization"]
):
    fixture, client = assistant_creation
    match failure:
        case "preflight":
            fixture.start.side_effect = GuestStartPreflightError("configuration unavailable")
        case "lifecycle":
            monkeypatch.setattr(
                onboarding_api,
                "start_agent_guest",
                AsyncMock(return_value=LifecycleFailure(502, "guest_start_failed", "Guest could not be started")),
            )
        case "initialization":
            fixture.initialize.side_effect = OSError("disk unavailable")
        case unreachable:
            assert_never(unreachable)
    response = await client.post("/api/onboarding/personal-assistant", json={"name": "Retryable"})
    assert response.status_code == 200
    assert response.json()["start"]["status"] == "failed"
    assert response.json()["start"]["code"] == "guest_start_failed"
    assert isinstance(response.json()["start"]["message"], str)
    assert len(fixture.store.agent_rows) == 1
    assert fixture.rows[0].personal_assistant_agent_id == fixture.store.agent_rows[0].id
    assert fixture.store.agent_rows[0].status == "error"
    assert fixture.store.flush.await_count >= 2


async def test_pointer_failure_rolls_back_before_any_guest_work(assistant_creation):
    fixture, client = assistant_creation
    fixture.fail_pointer = True
    with pytest.raises(RuntimeError, match="Pointer insert failed"):
        await client.post("/api/onboarding/personal-assistant", json={"name": "Atomic"})
    assert fixture.rows == fixture.store.agent_rows == fixture.store.participants == fixture.store.grants == []
    fixture.initialize.assert_not_awaited()
    fixture.start.assert_not_awaited()


async def test_failed_retry_does_not_overwrite_a_concurrently_running_guest(assistant_creation, monkeypatch):
    fixture, client = assistant_creation
    actor = fixture.store.actor
    assert actor.tenant_id is not None
    agent = AgentRecord(id=uuid.uuid4(), creator_id=actor.id, name="Running", status=AgentStatus.RUNNING)
    fixture.store.agent_rows.append(agent)
    fixture.rows.append(
        UserTenantOnboardingRecord(
            id=uuid.uuid4(),
            user_id=actor.id,
            tenant_id=actor.tenant_id,
            personal_assistant_agent_id=agent.id,
            status="completed",
        )
    )
    monkeypatch.setattr(
        onboarding_api,
        "start_agent_guest",
        AsyncMock(return_value=LifecycleFailure(502, "guest_start_failed", "Guest preparation failed")),
    )
    response = await client.post("/api/onboarding/personal-assistant", json={"name": "Retry"})
    assert response.status_code == 200
    assert response.json()["start"]["status"] == "failed"
    assert fixture.store.agent_rows[0].status == "running"
    assert response.json()["onboarding"]["status"] == "completed"


async def test_concurrent_submissions_create_one_assistant_and_reuse_its_pointer(assistant_creation):
    fixture, client = assistant_creation
    fixture.store.actor.quota_max_agents = 1
    responses = []

    async def submit():
        responses.append(await client.post("/api/onboarding/personal-assistant", json={"name": "Once"}))

    async with anyio.create_task_group() as group:
        group.start_soon(submit)
        group.start_soon(submit)
    assert [response.status_code for response in responses] == [200, 200]
    assert len(fixture.store.agent_rows) == 1
    assert {response.json()["agent"]["id"] for response in responses} == {str(fixture.store.agent_rows[0].id)}
    assert fixture.start.await_count == 2


@pytest.mark.parametrize("rejection", ["quota", "inactive"])
async def test_quota_or_fresh_authorization_rejects_before_agent_writes(assistant_creation, rejection):
    fixture, client = assistant_creation
    if rejection == "quota":
        fixture.store.actor.quota_max_agents = 0
    else:
        fixture.store.actor.is_active = False
    response = await client.post("/api/onboarding/personal-assistant", json={"name": "Forbidden"})
    assert response.status_code == 403
    assert fixture.store.agent_rows == fixture.store.participants == fixture.store.grants == []
    fixture.initialize.assert_not_awaited()
    fixture.start.assert_not_awaited()
