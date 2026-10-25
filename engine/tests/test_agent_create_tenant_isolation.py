from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.records.user import UserRecord
from app.schemas.schemas import AgentCreate
from app.services import agent_provisioning
from test_agent_create_departments import creation

__all__ = ["creation"]


@pytest.mark.parametrize("scope", ["company", "user", "custom"])
@pytest.mark.parametrize("invalid_kind", ["missing", "foreign", "tenantless"])
async def test_create_rejects_invalid_permission_users_before_any_write(creation, monkeypatch, scope, invalid_kind):
    # Given a valid user followed by a missing or out-of-tenant permission user.
    store, client = creation
    valid = UserRecord(id=uuid4(), tenant_id=store.actor.tenant_id)
    invalid = UserRecord(id=uuid4(), tenant_id=uuid4() if invalid_kind == "foreign" else None)
    users = [valid] if invalid_kind == "missing" else [valid, invalid]
    monkeypatch.setattr(agent_provisioning.user_dao, "get_many", AsyncMock(return_value=users))

    # When the HTTP create boundary receives the complete selection.
    response = await client.post(
        "/api/agents/",
        json={
            "name": "Tenant isolated agent",
            "permission_scope_type": scope,
            "permission_scope_ids": [str(valid.id), str(invalid.id)],
        },
    )

    # Then no row, key, relationship, or setup work has been written.
    assert response.status_code == 422
    assert store.events == ["begin", "rollback"]
    assert store.agent_rows == store.participants == store.grants == []
    store.mint_key.assert_not_called()
    store.write_key.assert_not_called()
    store.relationships.assert_not_awaited()
    store.flush.assert_not_awaited()
    store.setup.assert_not_awaited()


@pytest.mark.parametrize("invalid_kind", ["missing", "foreign"])
async def test_shared_provisioning_rejects_invalid_permission_users(creation, monkeypatch, invalid_kind):
    # Given a non-HTTP caller of the shared creation service.
    store, _client = creation
    invalid = UserRecord(id=uuid4(), tenant_id=uuid4())
    users = [] if invalid_kind == "missing" else [invalid]
    monkeypatch.setattr(agent_provisioning.user_dao, "get_many", AsyncMock(return_value=users))
    context = agent_provisioning.AgentProvisioning(store.actor, store.actor.tenant_id, None)
    data = AgentCreate(name="Service agent", permission_scope_type="user", permission_scope_ids=[invalid.id])

    # When it provisions an agent without passing through the HTTP handler.
    with pytest.raises(HTTPException) as error:
        async with store.transaction():
            await agent_provisioning.provision_agent(context, data)

    # Then the shared boundary rejects the selection before creation.
    assert error.value.status_code == 422
    assert store.events == ["begin", "rollback"]
    assert store.agent_rows == store.participants == store.grants == []


@pytest.mark.parametrize("role", ["platform_admin", "org_admin"])
@pytest.mark.parametrize("selection", ["target_tenant", "actor_tenant"])
async def test_creation_permission_users_follow_resolved_admin_target(creation, monkeypatch, role, selection):
    # Given an existing authorized admin target override and a selected user.
    store, client = creation
    store.actor = replace(store.actor, role=role)
    target_tenant_id = uuid4()
    selected = UserRecord(
        id=uuid4(), tenant_id=target_tenant_id if selection == "target_tenant" else store.actor.tenant_id
    )
    monkeypatch.setattr(agent_provisioning.user_dao, "get_many", AsyncMock(return_value=[selected]))

    # When the admin creates in the target tenant rather than their own.
    response = await client.post(
        "/api/agents/",
        json={
            "name": "Target tenant agent",
            "tenant_id": str(target_tenant_id),
            "permission_scope_type": "user",
            "permission_scope_ids": [str(selected.id)],
        },
    )

    # Then only target-tenant users are accepted.
    assert response.status_code == (201 if selection == "target_tenant" else 422)
    if selection == "target_tenant":
        assert store.agent_rows[0].tenant_id == target_tenant_id
        assert [(grant.scope_id, grant.access_level) for grant in store.grants] == [(selected.id, "use")]
    else:
        assert store.events == ["begin", "rollback"]
        assert store.agent_rows == store.participants == store.grants == []


@pytest.mark.parametrize("greeting", [None, "", "Hello, team!\nHow can I help?"])
async def test_creation_persists_requested_welcome_message(creation, monkeypatch, greeting):
    # Given an optional greeting, including the existing empty-to-null read normalization.
    store, client = creation
    create_row = AsyncMock(wraps=store.create_agent)
    monkeypatch.setattr(agent_provisioning.agent_dao, "create", create_row)

    # When creation persists the agent and schedules its setup.
    response = await client.post("/api/agents/", json={"name": "Greeting agent", "welcome_message": greeting})

    # Then persistence receives the exact input and the response retains normalized record semantics.
    assert response.status_code == 201
    assert create_row.await_args_list[0].kwargs["obj_in"]["welcome_message"] == greeting
    assert store.agent_rows[0].welcome_message == (greeting or None)
    assert response.json()["welcome_message"] == (greeting or None)
