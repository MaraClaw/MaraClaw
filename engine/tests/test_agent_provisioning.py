from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, assert_never, override
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import anyio
import pytest

from app.api import onboarding
from app.db import session
from app.db.connection import DbConnection
from app.records.tenant import TenantRecord
from app.services import agent_lifecycle, agent_provisioning, quota_guard
from test_agent_create_departments import creation
from test_onboarding import AssistantCreation, assistant_creation

__all__ = ["assistant_creation", "creation"]


class AssistantCommitConnection(DbConnection):
    def __init__(self, fixture: AssistantCreation):
        self.fixture: AssistantCreation = fixture
        self.snapshots: list[tuple[list[UUID], UUID | None]] = []

    @override
    async def commit(self) -> None:
        self.fixture.store.in_transaction = False
        self.fixture.store.events.append("commit")
        self.snapshots.append(
            (
                [agent.id for agent in self.fixture.store.agent_rows],
                self.fixture.rows[0].personal_assistant_agent_id if self.fixture.rows else None,
            )
        )

    @override
    async def rollback(self) -> None:
        self.fixture.store.agent_rows.clear()
        self.fixture.rows.clear()


@pytest.mark.parametrize("failure", [False, True])
async def test_request_bound_first_commit_contains_agent_and_pointer_before_initialization(
    assistant_creation, monkeypatch, failure
):
    fixture, _client = assistant_creation
    db = AssistantCommitConnection(fixture)
    monkeypatch.setattr(agent_provisioning, "connection_ctx", session.connection_ctx)
    monkeypatch.setattr(onboarding, "flush_request_transaction", session.flush_request_transaction)
    monkeypatch.setattr(agent_lifecycle, "flush_request_transaction", session.flush_request_transaction)

    async def initialize(agent, **_kwargs):
        assert db.snapshots == [([agent.id], agent.id)]
        if failure:
            raise OSError("disk unavailable")

    fixture.initialize.side_effect = initialize
    fixture.store.in_transaction = True
    async with session.transaction(db):
        response = await onboarding.create_personal_assistant(
            onboarding.PersonalAssistantRequest(name="Committed"), fixture.store.actor
        )
    agent = fixture.store.agent_rows[0]
    assert response["start"]["status"] == ("failed" if failure else "started")
    assert db.snapshots[0] == ([agent.id], agent.id)
    assert agent.status == ("error" if failure else "running")


async def test_regular_concurrent_creations_cannot_both_take_last_quota_slot(creation, monkeypatch):
    store, client = creation
    store.actor.quota_max_agents = 1
    mutex = anyio.Lock()

    @asynccontextmanager
    async def serialized_transaction() -> AsyncGenerator[None]:
        async with mutex, store.transaction():
            yield

    monkeypatch.setattr(agent_provisioning, "connection_ctx", serialized_transaction)
    monkeypatch.setattr(agent_provisioning, "check_agent_creation_quota", quota_guard.check_agent_creation_quota)
    monkeypatch.setattr(
        agent_provisioning.agent_dao,
        "count_active_for_creator",
        AsyncMock(side_effect=lambda _id: len(store.agent_rows)),
    )
    responses = []

    async def submit():
        responses.append(await client.post("/api/agents/", json={"name": "Last slot"}))

    async with anyio.create_task_group() as group:
        group.start_soon(submit)
        group.start_soon(submit)
    assert sorted(response.status_code for response in responses) == [201, 403]
    assert len(store.agent_rows) == 1
    assert store.setup.await_count == 1


@pytest.mark.parametrize("rejection", ["quota", "inactive", "password", "moved"])
async def test_regular_creation_uses_fresh_principal_not_dependency_snapshot(
    creation, monkeypatch, rejection: Literal["quota", "inactive", "password", "moved"]
):
    store, client = creation
    from app.records.identity import IdentityRecord

    fresh = replace(store.actor)
    match rejection:
        case "quota":
            fresh.quota_max_agents = 0
        case "inactive":
            fresh.is_active = False
        case "password":
            fresh.identity = IdentityRecord(id=uuid4(), email="member@example.test", must_change_password=True)
        case "moved":
            fresh.tenant_id = UUID(int=999)
        case unreachable:
            assert_never(unreachable)
    monkeypatch.setattr(agent_provisioning.user_dao, "get_with_identity", AsyncMock(return_value=fresh))
    monkeypatch.setattr(agent_provisioning, "check_agent_creation_quota", quota_guard.check_agent_creation_quota)
    monkeypatch.setattr(agent_provisioning.agent_dao, "count_active_for_creator", AsyncMock(return_value=0))

    response = await client.post("/api/agents/", json={"name": "Fresh only"})

    assert response.status_code == (409 if rejection == "moved" else 403)
    assert store.agent_rows == store.participants == store.grants == []
    store.setup.assert_not_awaited()


@pytest.mark.parametrize("heartbeat", [120, 480])
async def test_regular_creation_preserves_tenant_resource_defaults(creation, monkeypatch, heartbeat):
    store, client = creation
    tenant_id = store.actor.tenant_id
    assert tenant_id is not None
    tenant = TenantRecord(
        id=tenant_id,
        name="Company",
        slug="company",
        default_agent_ttl_hours=9,
        default_max_llm_calls_per_day=43,
        default_max_triggers=8,
        min_poll_interval_floor=12,
        max_webhook_rate_ceiling=17,
        min_heartbeat_interval_minutes=heartbeat,
    )
    monkeypatch.setattr(agent_provisioning.tenant_dao, "get", AsyncMock(return_value=tenant))
    before = datetime.now(UTC)

    response = await client.post("/api/agents/", json={"name": "Defaults"})

    assert response.status_code == 201
    agent = store.agent_rows[0]
    assert agent.setup_pending
    assert agent.expires_at is not None
    assert 8.99 < (agent.expires_at - before).total_seconds() / 3600 < 9.01
    assert (
        agent.max_llm_calls_per_day,
        agent.max_triggers,
        agent.min_poll_interval_min,
        agent.webhook_rate_limit,
        agent.heartbeat_interval_minutes,
    ) == (43, 8, 12, 17, max(240, heartbeat))


async def test_missing_tenant_defaults_use_fresh_actor_ttl_and_regular_fallbacks(creation, monkeypatch):
    store, client = creation
    fresh = replace(store.actor, quota_agent_ttl_hours=6)
    monkeypatch.setattr(agent_provisioning.user_dao, "get_with_identity", AsyncMock(return_value=fresh))
    before = datetime.now(UTC)

    response = await client.post("/api/agents/", json={"name": "Fallbacks"})

    assert response.status_code == 201
    agent = store.agent_rows[0]
    assert agent.expires_at is not None
    assert 5.99 < (agent.expires_at - before).total_seconds() / 3600 < 6.01
    assert (
        agent.max_llm_calls_per_day,
        agent.max_triggers,
        agent.min_poll_interval_min,
        agent.webhook_rate_limit,
        agent.heartbeat_interval_minutes,
    ) == (1000, 20, 5, 5, 240)
