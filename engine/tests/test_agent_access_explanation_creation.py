from dataclasses import replace
from uuid import uuid4

import pytest

from app.core.domain.users import UserRole

pytest_plugins = ["test_agent_create_departments"]


@pytest.mark.parametrize("scope", ["company", "user", "custom"])
async def test_create_response_uses_creator_reason_for_all_sharing_modes(creation, scope: str) -> None:
    store, client = creation

    response = await client.post("/api/agents/", json={"name": "Explain access", "permission_scope_type": scope})

    assert response.status_code == 201
    assert response.json()["access_reason"] == "creator"
    assert response.json()["access_level"] == "manage"
    assert store.actor.id == store.agent_rows[0].creator_id


@pytest.mark.parametrize("role", [UserRole.ORG_ADMIN, UserRole.PLATFORM_ADMIN])
async def test_cross_tenant_creation_does_not_invent_an_access_reason(creation, role: UserRole) -> None:
    store, client = creation
    store.actor = replace(store.actor, role=role)

    response = await client.post("/api/agents/", json={"name": "Foreign provision", "tenant_id": str(uuid4())})

    assert response.status_code == 201
    assert response.json()["access_reason"] is None
    assert response.json()["access_level"] == "manage"
