from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import agents
from app.records.agent import AgentPermissionRecord, AgentRecord
from app.records.org_department import OrgDepartmentRecord
from app.records.user import UserRecord
from app.schemas.schemas import AgentCreate
from app.services import onboarding
from openclaw_update_gate_fixtures import open_guest_write_gate


@dataclass(slots=True)
class CreationStore:
    actor: UserRecord
    departments: dict[UUID, OrgDepartmentRecord]
    agent_rows: list[AgentRecord] = field(default_factory=list)
    participants: list[dict[str, object]] = field(default_factory=list)
    grants: list[AgentPermissionRecord] = field(default_factory=list)
    lookups: list[list[UUID]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    fail_department_id: UUID | None = None
    in_transaction: bool = False
    mint_key: Mock = field(default_factory=lambda: Mock(return_value=("test-key", "test-hash")))
    write_key: Mock = field(default_factory=Mock)
    relationships: AsyncMock = field(default_factory=AsyncMock)
    flush: AsyncMock = field(default_factory=AsyncMock)
    setup: AsyncMock = field(default_factory=AsyncMock)

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        self.events.append("begin")
        try:
            yield
        except RuntimeError, HTTPException:
            self.agent_rows.clear()
            self.participants.clear()
            self.grants.clear()
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
        finally:
            self.in_transaction = False

    async def get_departments(self, ids: Sequence[UUID]) -> list[OrgDepartmentRecord]:
        assert self.in_transaction
        self.lookups.append(list(ids))
        return [self.departments[department_id] for department_id in ids if department_id in self.departments]

    async def create_agent(self, *, obj_in: Mapping[str, object]) -> AgentRecord:
        assert self.in_transaction
        row = AgentRecord.from_row({"id": uuid4(), "created_at": datetime.now(UTC), **obj_in})
        self.agent_rows.append(row)
        self.events.append("agent")
        return row

    async def create_participant(self, *, obj_in: Mapping[str, object]) -> None:
        assert self.in_transaction
        self.participants.append(dict(obj_in))
        self.events.append("participant")

    async def create_grant(self, *, obj_in: Mapping[str, object]) -> AgentPermissionRecord:
        assert self.in_transaction
        row = AgentPermissionRecord.from_row({"id": uuid4(), **obj_in})
        if row.scope_type == "department" and row.scope_id == self.fail_department_id:
            raise RuntimeError("Department insert failed")
        self.grants.append(row)
        self.events.append(row.scope_type)
        return row

    async def update_agent(self, *, db_obj: AgentRecord, obj_in: Mapping[str, object]) -> AgentRecord:
        return replace(db_obj, api_key_hash=str(obj_in["api_key_hash"]))

    async def persist_setup(self, agent: AgentRecord, data: AgentCreate, raw_key: str) -> None:
        assert self.in_transaction
        assert agent.setup_pending
        assert agent.api_key_hash == "test-hash"
        assert raw_key == "test-key"
        await self.setup(agent, data, raw_key)
        self.events.append("setup")


@pytest.fixture
async def creation(monkeypatch):
    tenant_id = uuid4()
    department = OrgDepartmentRecord(uuid4(), tenant_id, "Sales", datetime.now(UTC))
    mint_key = Mock(return_value=("test-key", "test-hash"))
    write_key = Mock()
    relationships = AsyncMock()
    flush = AsyncMock()
    setup = AsyncMock()
    store = CreationStore(
        UserRecord(id=uuid4(), tenant_id=tenant_id),
        {department.id: department},
        mint_key=mint_key,
        write_key=write_key,
        relationships=relationships,
        flush=flush,
        setup=setup,
    )
    monkeypatch.setattr(agents, "connection_ctx", store.transaction)
    monkeypatch.setattr(agents, "lock_tenants", AsyncMock())
    monkeypatch.setattr(agents, "lock_user", AsyncMock())

    async def fresh_actor(_user_id: UUID, *, fresh: bool = False) -> UserRecord:
        assert fresh
        assert store.in_transaction
        return store.actor

    monkeypatch.setattr(agents.user_dao, "get_with_identity", fresh_actor)
    monkeypatch.setattr(agents.agent_dao, "create", store.create_agent)
    monkeypatch.setattr(agents.agent_dao, "update", store.update_agent)
    monkeypatch.setattr(agents.participant_dao, "create", store.create_participant)
    monkeypatch.setattr(agents.agent_permission_dao, "create", store.create_grant)
    monkeypatch.setattr(agents.org_department_dao, "get_many", store.get_departments)
    monkeypatch.setattr(agents.tenant_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(agents, "check_agent_creation_quota", AsyncMock())
    monkeypatch.setattr(agents, "assert_models_in_tenant_pool", AsyncMock())
    monkeypatch.setattr(agents, "ensure_access_granted_platform_relationships", relationships)
    monkeypatch.setattr(agents, "mint_openclaw_gateway_key", mint_key)
    monkeypatch.setattr(agents, "write_gateway_api_key", write_key)
    monkeypatch.setattr(agents, "flush_request_transaction", flush)
    monkeypatch.setattr(agents, "persist_agent_setup", store.persist_setup)
    monkeypatch.setattr(onboarding, "is_onboarded", AsyncMock(return_value=False))
    app = FastAPI()
    app.include_router(agents.router, prefix="/api")
    app.dependency_overrides[agents.get_current_user] = lambda: store.actor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield store, client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("access_level", ["use", "manage"])
async def test_create_custom_departments_are_deduplicated_use_only_with_creator_manage(creation, access_level):
    store, client = creation
    first = next(iter(store.departments.values()))
    second = replace(first, id=uuid4(), name="Support")
    store.departments[second.id] = second
    response = await client.post(
        "/api/agents/",
        json={
            "name": "Sales agent",
            "permission_scope_type": "custom",
            "permission_access_level": access_level,
            "department_ids": [str(second.id), str(first.id), str(second.id)],
        },
    )
    assert response.status_code == 201
    assert response.json()["access_mode"] == "custom"
    assert [(row.scope_type, row.scope_id, row.access_level) for row in store.grants] == [
        ("user", store.actor.id, "manage"),
        *(("department", department_id, "use") for department_id in sorted(store.departments)),
    ]
    assert len(store.lookups) == 1
    assert set(store.lookups[0]) == set(store.departments)
    assert len(store.lookups[0]) == 2
    assert all(str(row.agent_id) == response.json()["id"] for row in store.grants)
    assert store.events == ["begin", "agent", "participant", "user", "department", "department", "setup", "commit"]


@pytest.mark.parametrize("action", ["start", "api-key"])
async def test_pending_setup_rejects_start_and_key_rotation(creation, monkeypatch, action):
    open_guest_write_gate(monkeypatch)
    store, client = creation
    agent = AgentRecord(id=uuid4(), name="Pending", creator_id=store.actor.id, setup_pending=True)
    monkeypatch.setattr(agents, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    response = await client.post(f"/api/agents/{agent.id}/{action}")
    assert response.status_code == 409
    store.mint_key.assert_not_called()
    store.write_key.assert_not_called()


@pytest.mark.parametrize("invalid_kind", ["missing", "foreign"])
async def test_invalid_department_rejects_all_creation_side_effects(creation, invalid_kind):
    store, client = creation
    valid = next(iter(store.departments.values()))
    invalid_id = uuid4()
    if invalid_kind == "foreign":
        store.departments[invalid_id] = replace(valid, id=invalid_id, tenant_id=uuid4())
    response = await client.post(
        "/api/agents/",
        json={
            "name": "Sales agent",
            "permission_scope_type": "custom",
            "department_ids": [str(valid.id), str(invalid_id)],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid department for this organization"
    assert store.events == ["begin", "rollback"]
    assert store.agent_rows == store.participants == store.grants == []
    store.mint_key.assert_not_called()
    store.write_key.assert_not_called()
    store.relationships.assert_not_awaited()
    store.flush.assert_not_awaited()
    store.setup.assert_not_awaited()


@pytest.mark.parametrize("scope", ["company", "user"])
async def test_noncustom_departments_reject_before_any_write(creation, scope):
    store, client = creation
    response = await client.post(
        "/api/agents/",
        json={"name": "Sales agent", "permission_scope_type": scope, "department_ids": [str(uuid4())]},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Department grants require custom sharing"
    assert store.events == store.lookups == []


@pytest.mark.parametrize("scope", [None, "company", "user", "custom"])
@pytest.mark.parametrize("selection", ["omitted", "empty"])
async def test_empty_departments_preserve_existing_sharing_defaults(creation, scope, selection):
    store, client = creation
    payload: dict[str, str | list[str]] = {"name": "Sales agent"}
    if scope is not None:
        payload["permission_scope_type"] = scope
    if selection == "empty":
        payload["department_ids"] = []
    response = await client.post("/api/agents/", json=payload)
    expected_scope = "company" if scope in (None, "company") else "user"
    assert response.status_code == 201
    assert [(row.scope_type, row.scope_id, row.access_level) for row in store.grants] == [
        (expected_scope, None, "use") if expected_scope == "company" else ("user", store.actor.id, "manage")
    ]
    assert store.lookups == []


def test_department_defaults_are_independent_uuid_lists():
    first = AgentCreate(name="First agent")
    second = AgentCreate(name="Second agent")
    first.department_ids.append(uuid4())
    assert second.department_ids == []


@pytest.mark.parametrize("department_ids", [["invalid-uuid"], None, "invalid-list"])
async def test_malformed_department_ids_reject_at_request_boundary(creation, department_ids):
    store, client = creation
    response = await client.post(
        "/api/agents/",
        json={"name": "Sales agent", "permission_scope_type": "custom", "department_ids": department_ids},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", "department_ids"]
    assert store.events == store.lookups == []


@pytest.mark.parametrize("role", ["platform_admin", "org_admin"])
@pytest.mark.parametrize("selection", ["target", "actor"])
async def test_admin_override_validates_departments_against_target_tenant(creation, role, selection):
    store, client = creation
    store.actor = replace(store.actor, role=role)
    own_department = next(iter(store.departments.values()))
    target_id = uuid4()
    target_department = replace(own_department, id=uuid4(), tenant_id=target_id)
    store.departments[target_department.id] = target_department
    selected = target_department if selection == "target" else own_department
    response = await client.post(
        "/api/agents/",
        json={
            "name": "Sales agent",
            "tenant_id": str(target_id),
            "permission_scope_type": "custom",
            "department_ids": [str(selected.id)],
        },
    )
    assert response.status_code == (201 if selection == "target" else 422)
    if selection == "target":
        assert store.agent_rows[0].tenant_id == target_id
        assert store.grants[-1].scope_id == target_department.id
    else:
        assert response.json()["detail"] == "Invalid department for this organization"
        assert store.events == ["begin", "rollback"]


async def test_department_insert_failure_rolls_back_agent_participant_and_all_grants(creation):
    store, client = creation
    first = next(iter(store.departments.values()))
    second = replace(first, id=uuid4())
    store.departments[second.id] = second
    store.fail_department_id = max(store.departments)
    with pytest.raises(RuntimeError, match="Department insert failed"):
        await client.post(
            "/api/agents/",
            json={
                "name": "Sales agent",
                "permission_scope_type": "custom",
                "department_ids": [str(first.id), str(second.id)],
            },
        )
    assert store.agent_rows == store.participants == store.grants == []
    assert store.events == ["begin", "agent", "participant", "user", "department", "rollback"]
    store.mint_key.assert_not_called()
    store.setup.assert_not_awaited()


async def test_setup_persistence_failure_rolls_back_creation_before_first_commit(creation):
    store, client = creation
    store.setup.side_effect = RuntimeError("Setup insert failed")
    with pytest.raises(RuntimeError, match="Setup insert failed"):
        await client.post("/api/agents/", json={"name": "Durable agent"})
    assert store.agent_rows == store.participants == store.grants == []
    assert store.events == ["begin", "agent", "participant", "company", "rollback"]
    store.flush.assert_not_awaited()
    store.write_key.assert_not_called()


async def test_create_returns_one_time_key_without_running_setup_in_request(creation):
    store, client = creation
    response = await client.post("/api/agents/", json={"name": "Durable agent"})
    assert response.status_code == 201
    assert response.json()["api_key"] == "test-key"
    assert response.json()["status"] == "idle"
    assert response.json()["access_level"] == "manage"
    assert "setup_pending" not in response.json()
    assert response.json()["api_key_hash"] == "test-hash"
    assert store.events == ["begin", "agent", "participant", "company", "setup", "commit"]
    store.setup.assert_awaited_once()
    store.write_key.assert_not_called()
