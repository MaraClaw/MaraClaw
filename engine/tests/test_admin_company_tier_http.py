from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import admin_company_tier
from app.core import security
from app.core.domain.users import UserRole
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from membership_generation_fixtures import pin_membership_generation
from test_organization_tier_changes import TENANT, store

__all__ = ["store"]
PATH = f"/api/admin/companies/{TENANT}/tier"
HEADERS = {"Authorization": "Bearer t"}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(admin_company_tier.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://tier.test") as client:
        yield client


@pytest.fixture
def principal(monkeypatch) -> UserRecord:
    identity = IdentityRecord(id=uuid4())
    user = UserRecord(id=uuid4(), identity_id=identity.id, identity=identity, role=UserRole.PLATFORM_ADMIN)
    monkeypatch.setattr(security, "decode_access_token", lambda _: {"sub": str(user.id), "gen": 1, "mgen": 0})
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=1))
    pin_membership_generation(monkeypatch)
    return user


async def test_preview_returns_canonical_policy_as_json(client, principal, store):
    response = await client.post(f"{PATH}/preview", json={"target_tier": "pro"}, headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["current_tier"], body["target_tier"], body["per_account_agent_limit"]) == ("max", "pro", 2)
    assert body["next_capacity_renewal"]["billing_month"] == "2027-01-01"
    assert body["guest_limits_apply_on"] == "stop_resume_or_replacement"
    assert body["guest_observation_available"] is True
    assert body["existing_orders_keep_recorded_tier"] is True


async def test_apply_returns_changed_tier_and_episode_rollover(client, principal, store):
    response = await client.patch(PATH, json={"expected_tier": "max", "target_tier": "pro"}, headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "tenant_id": str(TENANT),
        "previous_tier": "max",
        "tier": "pro",
        "changed": True,
        "closed_pressure_episodes": 4,
    }


async def test_apply_conflict_is_a_coded_409(client, principal, store):
    response = await client.patch(PATH, json={"expected_tier": "pro", "target_tier": "ultra"}, headers=HEADERS)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "company_tier_changed"
    store["update"].assert_not_awaited()


@pytest.mark.parametrize(("method", "suffix"), [("POST", "/preview"), ("PATCH", "")])
@pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.AGENT_ADMIN, UserRole.ORG_ADMIN])
async def test_only_platform_admin_can_preview_or_apply(client, principal, store, method, suffix, role):
    principal.role = role
    data = {"target_tier": "pro"}
    if method == "PATCH":
        data["expected_tier"] = "max"

    response = await client.request(method, PATH + suffix, json=data, headers=HEADERS)

    assert response.status_code == 403
    store["tenants"].assert_not_awaited()


@pytest.mark.parametrize(("method", "suffix"), [("POST", "/preview"), ("PATCH", "")])
async def test_force_password_change_still_blocks_tier_routes(client, principal, store, method, suffix):
    assert principal.identity is not None
    principal.identity.must_change_password = True

    response = await client.request(
        method,
        PATH + suffix,
        json={"target_tier": "pro", "expected_tier": "max"} if suffix == "" else {"target_tier": "pro"},
        headers=HEADERS,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["must_change_password"] is True


@pytest.mark.parametrize(
    "data",
    [
        {"target_tier": "free", "expected_tier": "max"},
        {"target_tier": "pro"},
        {"target_tier": "pro", "expected_tier": "max", "force": True},
    ],
)
async def test_apply_requires_known_tiers_and_rejects_extra_fields(client, principal, store, data):
    response = await client.patch(PATH, json=data, headers=HEADERS)

    assert response.status_code == 422
    store["update"].assert_not_awaited()


async def test_system_company_refusal_is_exposed_without_writes(client, principal, store):
    store["tenants"].return_value[0].is_system = True

    response = await client.patch(PATH, json={"expected_tier": "max", "target_tier": "pro"}, headers=HEADERS)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "system_company_tier_fixed"
    store["update"].assert_not_awaited()
