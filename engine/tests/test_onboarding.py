import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import onboarding as onboarding_api
from app.core.domain.agents import AgentStatus
from app.records.agent import AgentRecord
from app.records.onboarding import UserTenantOnboardingRecord
from app.services import agent_lifecycle, agent_provisioning, onboarding as onboarding_mod, quota_guard
from app.services.agent_lifecycle import LifecycleSuccess
from app.services.guest_start import start_operation_id
from app.services.onboarding import (
    PHASE_CUSTOM_STYLE,
    PHASE_GREETED,
    PHASE_TEMPLATE_FOCUS,
    resolve_onboarding_prompt,
    try_begin_onboarding_greeting,
)
from onboarding_start_fakes import AssistantStartStore
from test_agent_create_departments import CreationStore, creation

__all__ = ["creation"]


@dataclass(slots=True)
class AssistantCreation:
    store: CreationStore
    rows: list[UserTenantOnboardingRecord] = field(default_factory=list)
    initialize: Callable[..., Awaitable[None]] = field(default_factory=AsyncMock)
    start: Callable[[AgentRecord], Awaitable[None]] = field(default_factory=AsyncMock)
    write_key: Callable[[AgentRecord, str], None] = field(default_factory=Mock)
    fail_pointer: bool = False

    async def get_row(self, _user_id: UUID, _tenant_id: UUID) -> UserTenantOnboardingRecord | None:
        return self.rows[0] if self.rows else None

    async def insert_row(self, *, user_id: UUID, tenant_id: UUID, **_values: object) -> None:
        assert self.store.in_transaction
        self.rows.append(UserTenantOnboardingRecord(id=uuid4(), user_id=user_id, tenant_id=tenant_id))

    async def update_row(
        self, *, db_obj: UserTenantOnboardingRecord, obj_in: Mapping[str, object]
    ) -> UserTenantOnboardingRecord:
        assert self.store.in_transaction
        if self.fail_pointer and "personal_assistant_agent_id" in obj_in:
            raise RuntimeError("Pointer insert failed")
        values = {**asdict(db_obj), **obj_in}
        row = UserTenantOnboardingRecord.from_row(values)
        self.rows[:] = [row]
        if "personal_assistant_agent_id" in obj_in:
            self.store.events.append("pointer")
        return row

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return next((replace(row) for row in self.store.agent_rows if row.id == agent_id), None)


@pytest.fixture
async def assistant_creation(creation, monkeypatch: pytest.MonkeyPatch):
    store, _client = creation
    fixture = AssistantCreation(store)
    starts = AssistantStartStore(store)
    mutex = anyio.Lock()

    @asynccontextmanager
    async def serialized_transaction() -> AsyncGenerator[None]:
        async with mutex:
            previous = list(fixture.rows)
            try:
                async with store.transaction():
                    yield
            except RuntimeError, HTTPException:
                fixture.rows[:] = previous
                raise

    @asynccontextmanager
    async def locked_agent(agent_id: UUID) -> AsyncGenerator[AgentRecord]:
        agent = await fixture.get_agent(agent_id)
        assert agent is not None
        yield agent

    async def initialize(agent: AgentRecord, **kwargs: object) -> None:
        assert not store.in_transaction
        assert fixture.rows[0].personal_assistant_agent_id == agent.id
        assert "commit" in store.events
        await fixture.initialize(agent, **kwargs)

    async def start(agent: AgentRecord) -> str | None:
        await fixture.start(agent)
        agent.status = AgentStatus.RUNNING
        agent.container_id = "owned-guest"
        operation_id = start_operation_id.get()
        assert operation_id is not None
        assert await starts.observe(operation_id, agent)
        return agent.container_id

    monkeypatch.setattr(agent_provisioning, "connection_ctx", serialized_transaction)
    monkeypatch.setattr(agent_provisioning, "check_agent_creation_quota", quota_guard.check_agent_creation_quota)
    monkeypatch.setattr(
        agent_provisioning.agent_dao,
        "count_active_for_creator",
        AsyncMock(side_effect=lambda _id: len(store.agent_rows)),
    )
    monkeypatch.setattr(onboarding_api.user_tenant_onboarding_dao, "get_for_user_tenant", fixture.get_row)
    monkeypatch.setattr(onboarding_api.user_tenant_onboarding_dao, "insert_ignore", fixture.insert_row)
    monkeypatch.setattr(onboarding_api.user_tenant_onboarding_dao, "update", fixture.update_row)
    monkeypatch.setattr(onboarding_api.agent_dao, "get", fixture.get_agent)
    monkeypatch.setattr(onboarding_api.agent_template_dao, "get_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(onboarding_api.llm_model_dao, "first_enabled_id_for_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr(onboarding_api.agent_manager, "initialize_agent_files", initialize)
    monkeypatch.setattr(onboarding_api.agent_manager, "start_container", start)
    monkeypatch.setattr(onboarding_api, "write_gateway_api_key", fixture.write_key)
    monkeypatch.setattr(onboarding_api, "flush_request_transaction", store.flush)
    monkeypatch.setattr(onboarding_api, "locked_fresh_agent", locked_agent)
    monkeypatch.setattr(agent_lifecycle, "locked_fresh_agent", locked_agent)
    monkeypatch.setattr(agent_lifecycle, "flush_request_transaction", store.flush)
    for method in (starts.begin, starts.publish, starts.recover, starts.observe, starts.reserve, starts.fail):
        monkeypatch.setattr(agent_lifecycle.start_store, method.__name__, method)
    monkeypatch.setattr("app.services.agent_start_reconciler.flush_request_transaction", store.flush)
    from app.api import relationships

    monkeypatch.setattr(relationships, "_regenerate_relationships_file", AsyncMock())
    app = FastAPI()
    app.include_router(onboarding_api.router, prefix="/api")
    app.dependency_overrides[onboarding_api.get_current_user] = lambda: store.actor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield fixture, client
    finally:
        app.dependency_overrides.clear()


def _make_agent(*, template_id: uuid.UUID | None = None) -> AgentRecord:
    return AgentRecord(
        id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        name="helper",
        role_description="assistant",
        template_id=template_id,
    )


class _FakeConn:
    def __init__(self, responses):
        self.responses: list[object] = list(responses)

    async def fetchone(self, _sql, _params=None):
        if not self.responses:
            raise AssertionError("unexpected fetchone")
        return self.responses.pop(0)

    async def fetchval(self, _sql, _params=None):
        if not self.responses:
            raise AssertionError("unexpected fetchval")
        return self.responses.pop(0)


class _Ctx:
    def __init__(self, conn):
        self.conn: _FakeConn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


def _patch_connection(monkeypatch, responses):
    conn = _FakeConn(responses)
    monkeypatch.setattr(onboarding_mod, "connection_ctx", lambda: _Ctx(conn))
    return conn


@pytest.mark.asyncio
async def test_first_contact_is_the_only_tool_free_greeting_turn(monkeypatch):
    # existing phase None, user_turns 0, peer_count 0
    _patch_connection(monkeypatch, [None, 0, 0])

    injection = await resolve_onboarding_prompt(
        _make_agent(),
        uuid.uuid4(),
        user_name="Ray",
        user_locale="zh",
    )

    assert injection is not None
    assert injection.is_greeting_turn is True


@pytest.mark.asyncio
async def test_template_follow_up_keeps_tools_enabled(monkeypatch):
    template_id = uuid.uuid4()
    _patch_connection(
        monkeypatch,
        [
            {"phase": PHASE_GREETED},
            1,  # user turns
            1,  # peer count
            {"capability_bullets": ["Install apps"], "bootstrap_content": "preset bootstrap"},
        ],
    )

    injection = await resolve_onboarding_prompt(
        _make_agent(template_id=template_id),
        uuid.uuid4(),
        user_name="Ray",
        user_locale="zh",
    )

    assert injection is not None
    assert injection.is_greeting_turn is False
    assert injection.target_phase == PHASE_TEMPLATE_FOCUS


@pytest.mark.asyncio
async def test_custom_follow_up_keeps_tools_enabled(monkeypatch):
    _patch_connection(
        monkeypatch,
        [
            {"phase": PHASE_GREETED},
            1,
            1,
        ],
    )

    injection = await resolve_onboarding_prompt(
        _make_agent(),
        uuid.uuid4(),
        user_name="Ray",
        user_locale="zh",
    )

    assert injection is not None
    assert injection.is_greeting_turn is False
    assert injection.target_phase == PHASE_CUSTOM_STYLE


@pytest.mark.asyncio
async def test_custom_boundary_follow_up_keeps_tools_enabled(monkeypatch):
    _patch_connection(
        monkeypatch,
        [
            {"phase": PHASE_CUSTOM_STYLE},
            2,
            1,
        ],
    )

    injection = await resolve_onboarding_prompt(
        _make_agent(),
        uuid.uuid4(),
        user_name="Ray",
        user_locale="zh",
    )

    assert injection is not None
    assert injection.is_greeting_turn is False
    assert injection.target_phase == onboarding_mod.PHASE_CUSTOM_BOUNDARIES


@pytest.mark.asyncio
async def test_try_begin_onboarding_greeting_claims_once(monkeypatch):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    _patch_connection(monkeypatch, [{"agent_id": agent_id}, None])

    assert await try_begin_onboarding_greeting(agent_id, user_id) is True
    assert await try_begin_onboarding_greeting(agent_id, user_id) is False


async def test_existing_assistant_is_started_again_with_typed_http_outcome(assistant_creation, monkeypatch):
    # Given an already persisted assistant whose earlier start can be retried.
    fixture, client = assistant_creation
    user = fixture.store.actor
    tenant_id = user.tenant_id
    assert tenant_id is not None
    agent = AgentRecord(id=uuid.uuid4(), name="Existing", creator_id=user.id, tenant_id=user.tenant_id)
    row = UserTenantOnboardingRecord(
        id=uuid.uuid4(), user_id=user.id, tenant_id=tenant_id, personal_assistant_agent_id=agent.id
    )
    fixture.rows.append(row)
    fixture.store.agent_rows.append(agent)
    fixture.store.actor.quota_max_agents = 0
    start = AsyncMock(return_value=LifecycleSuccess(agent))
    monkeypatch.setattr(onboarding_api, "start_agent_guest", start)
    # When submitting again through the actual HTTP surface.
    response = await client.post("/api/onboarding/personal-assistant", json={"name": "Ignored"})
    # Then a persisted outcome is HTTP 200, not another creation or a silent no-op.
    assert response.status_code == 200
    assert response.json()["start"] == {"status": "started"}
    assert response.json()["agent"] == {"id": str(agent.id), "name": agent.name}
    start.assert_awaited_once_with(agent.id)
    assert len(fixture.store.agent_rows) == 1
