from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter

from app.api import agents
from app.core.domain.agents import AgentStatus, PermissionAccessLevel
from app.core.json_types import JsonObject, JsonValue
from app.dao.agent_dao import AgentDAO
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.schemas.collaboration_response_agents import agent_model_dump
from app.schemas.schemas import AgentOut
from app.services import agent_provisioning, onboarding
from app.services.agent_lifecycle import LifecycleSuccess
from app.services.openclaw_keys import mint_openclaw_gateway_key


@pytest.fixture
async def surface(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[AsyncClient, AgentRecord]]:
    actor = UserRecord(uuid4(), tenant_id=uuid4())
    _, digest = mint_openclaw_gateway_key()
    agent = AgentRecord(
        uuid4(),
        "Keyed agent",
        actor.id,
        tenant_id=actor.tenant_id,
        api_key_hash=digest,
        status=AgentStatus.IDLE,
        timezone="UTC",
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )

    @asynccontextmanager
    async def provisioning(
        user: UserRecord, tenant_id: UUID | None
    ) -> AsyncGenerator[agent_provisioning.AgentProvisioning]:
        yield agent_provisioning.AgentProvisioning(user, tenant_id, None)

    async def update(*, db_obj: AgentRecord, obj_in: Mapping[str, JsonValue | UUID | datetime]) -> AgentRecord:
        for name, value in obj_in.items():
            setattr(db_obj, name, value)
        return db_obj

    monkeypatch.setattr(agents, "agent_provisioning", provisioning)
    monkeypatch.setattr(agent_provisioning, "check_agent_creation_quota", AsyncMock())
    monkeypatch.setattr(agent_provisioning, "ensure_access_granted_platform_relationships", AsyncMock())
    monkeypatch.setattr(agents.agent_dao, "create", AsyncMock(return_value=agent))
    monkeypatch.setattr(agents.agent_dao, "update", update)
    monkeypatch.setattr(agents.participant_dao, "create", AsyncMock())
    monkeypatch.setattr(agents.agent_permission_dao, "create", AsyncMock())
    monkeypatch.setattr(agents, "assert_models_in_tenant_pool", AsyncMock())
    monkeypatch.setattr(agents, "persist_agent_setup", AsyncMock())
    monkeypatch.setattr(agents, "flush_request_transaction", AsyncMock())
    monkeypatch.setattr(agents, "check_agent_access", AsyncMock(return_value=(agent, PermissionAccessLevel.MANAGE)))
    monkeypatch.setattr(agents, "_lazy_reset_token_counters", AsyncMock(return_value=False))
    monkeypatch.setattr(agents.user_dao, "get_with_identity", AsyncMock(return_value=actor))
    monkeypatch.setattr(agents, "list_visible_agents", AsyncMock(return_value=[agent]))
    monkeypatch.setattr(agents.agent_dao, "apply_token_counter_resets_many", AsyncMock())
    monkeypatch.setattr(agents, "_build_unread_count_by_agent", AsyncMock(return_value={}))
    monkeypatch.setattr(onboarding, "is_onboarded", AsyncMock(return_value=False))
    monkeypatch.setattr(onboarding, "onboarded_agent_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        agents,
        "start_agent_guest",
        AsyncMock(return_value=LifecycleSuccess(replace(agent, status=AgentStatus.RUNNING))),
    )
    monkeypatch.setattr(
        agents, "stop_agent_guest", AsyncMock(return_value=LifecycleSuccess(replace(agent, status=AgentStatus.STOPPED)))
    )
    app = FastAPI()
    app.include_router(agents.router, prefix="/api")
    app.dependency_overrides[agents.get_current_user] = lambda: actor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://key-contract") as client:
            yield client, agent
    finally:
        app.dependency_overrides.clear()


async def test_creation_returns_one_time_raw_key_without_hash(surface: tuple[AsyncClient, AgentRecord]) -> None:
    """Given real key minting, creation returns its token but never its digest."""
    client, agent = surface

    response = await client.post("/api/agents/", json={"name": agent.name})

    assert response.status_code == 201
    payload = TypeAdapter(JsonObject).validate_json(response.content)
    raw_key = TypeAdapter(str).validate_python(payload["api_key"])
    assert raw_key.startswith("oc-")
    assert sha256(raw_key.encode()).hexdigest() == agent.api_key_hash
    assert payload["has_api_key"] is True
    assert "api_key_hash" not in payload
    assert agent.api_key_hash is not None
    assert agent.api_key_hash not in response.text


@pytest.mark.parametrize(
    ("method", "suffix", "body", "listed"),
    [
        ("GET", "", None, False),
        ("GET", "/", None, True),
        ("PATCH", "", {"bio": "Updated"}, False),
        ("POST", "/start", None, False),
        ("POST", "/stop", None, False),
    ],
    ids=["get", "list", "update", "start", "stop"],
)
async def test_normal_responses_omit_credentials_when_agent_has_key(
    surface: tuple[AsyncClient, AgentRecord], method: str, suffix: str, body: JsonObject | None, listed: bool
) -> None:
    """Given a stored digest, every normal HTTP projection remains credential-free."""
    client, agent = surface
    path = "/api/agents/" if listed else f"/api/agents/{agent.id}{suffix}"

    response = await client.request(method, path, json=body)

    assert response.status_code == 200
    payloads = (
        TypeAdapter(list[JsonObject]).validate_json(response.content)
        if listed
        else [TypeAdapter(JsonObject).validate_json(response.content)]
    )
    assert len(payloads) == 1
    assert payloads[0]["id"] == str(agent.id)
    assert payloads[0]["has_api_key"] is True
    assert "api_key_hash" not in payloads[0]
    assert "api_key" not in payloads[0]
    assert agent.api_key_hash is not None
    assert agent.api_key_hash not in response.text


async def test_collaboration_dump_omits_credentials_when_agent_has_key(
    surface: tuple[AsyncClient, AgentRecord],
) -> None:
    """Given the same internal record, the unfiltered manual dump is also safe."""
    _, agent = surface

    payload = agent_model_dump(AgentOut.model_validate(agent))

    assert payload["has_api_key"] is True
    assert "api_key_hash" not in payload
    assert "api_key" not in payload


@dataclass(frozen=True, slots=True)
class KeyLookup:
    agent: AgentRecord

    async def fetchone(self, _sql: str, params: Mapping[str, str]) -> JsonObject | None:
        if params["key"] != self.agent.api_key_hash:
            return None
        return {
            "id": str(self.agent.id),
            "name": self.agent.name,
            "creator_id": str(self.agent.creator_id),
            "api_key_hash": self.agent.api_key_hash,
        }


@pytest.mark.parametrize("presented", ["token", "digest", "invalid"])
async def test_created_key_authenticates_only_as_raw_token(
    surface: tuple[AsyncClient, AgentRecord], monkeypatch: pytest.MonkeyPatch, presented: str
) -> None:
    """Given a real minted/stored pair, exercise the actual DAO lookup algorithm."""
    client, agent = surface
    response = await client.post("/api/agents/", json={"name": agent.name})
    assert response.status_code == 201
    raw_key = TypeAdapter(str).validate_python(TypeAdapter(JsonObject).validate_json(response.content)["api_key"])
    assert agent.api_key_hash is not None
    credential = {"token": raw_key, "digest": agent.api_key_hash, "invalid": "oc-invalid"}[presented]

    @asynccontextmanager
    async def session(_dao: AgentDAO) -> AsyncGenerator[KeyLookup]:
        yield KeyLookup(agent)

    monkeypatch.setattr(AgentDAO, "session", session)

    result = await AgentDAO().get_openclaw_by_api_key(credential)

    assert (result.id if result else None) == (agent.id if presented == "token" else None)


@pytest.mark.parametrize("operation", ["start", "stop"])
async def test_use_only_lifecycle_refusal_omits_credentials(
    surface: tuple[AsyncClient, AgentRecord], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Given use-only access, the error boundary reveals no stored credential."""
    client, agent = surface
    monkeypatch.setattr(agents, "check_agent_access", AsyncMock(return_value=(agent, PermissionAccessLevel.USE)))

    response = await client.post(f"/api/agents/{agent.id}/{operation}")

    assert response.status_code == 403
    assert agent.api_key_hash is not None
    assert agent.api_key_hash not in response.text
    assert "api_key" not in response.text
