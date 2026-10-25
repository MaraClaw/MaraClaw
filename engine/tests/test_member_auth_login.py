from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from tests.member_auth_fixtures import member_auth_setup

from app.api import auth
from app.core.domain.users import UserRole
from app.core.json_types import json_object_from, json_object_from_response


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("operator", ["identity", "role"])
async def test_member_login_rejects_operator_before_session_or_choices(monkeypatch, count, operator):
    # Given a platform operator, including one with an ordinary member membership.
    setup = member_auth_setup(monkeypatch, count=count)
    setup.identity.is_platform_admin = operator == "identity"
    if operator == "role":
        setup.users[-1].role = UserRole.PLATFORM_ADMIN
    # When the member route is called, regardless of an admin Origin.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/login",
            json={"login_identifier": "member@example.com", "password": "password"},
            headers={"Origin": "https://admin.example.com"},
        )
    # Then it rejects before minting or exposing a tenant-selection credential.
    assert response.status_code == 403
    assert response.json() == {
        "detail": {
            "code": "member_audience_rejected",
            "message": "Platform operators sign in on the admin console.",
        }
    }
    setup.mint.assert_not_awaited()
    setup.pending.assert_not_awaited()


@pytest.mark.parametrize("role", [UserRole.ORG_ADMIN, UserRole.AGENT_ADMIN, UserRole.MEMBER])
@pytest.mark.parametrize("forced", [False, True])
async def test_member_login_projects_eligible_roles_and_force_change(monkeypatch, role, forced):
    # Given an eligible role, with a password-change restriction when applicable.
    setup = member_auth_setup(monkeypatch)
    setup.users[0].role = role
    setup.identity.must_change_password = forced
    # When authenticated through the member route.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/login",
            json={"login_identifier": "member@example.com", "password": "password"},
        )
    # Then both nested records are member projections, with password restrictions intact.
    assert response.status_code == 200
    body = json_object_from_response(response)
    user = json_object_from(body["user"])
    identity = json_object_from(body["identity"])
    assert user["role"] == role
    assert user["must_change_password"] is forced
    assert identity["must_change_password"] is forced
    assert body["must_change_password"] is forced
    assert "is_platform_admin" not in user
    assert "is_platform_admin" not in identity
    assert "password_hash" not in user
    assert "password_hash" not in identity
    setup.mint.assert_awaited_once_with(str(setup.users[0].id), role)


async def test_member_login_keeps_wrong_password_unauthorized(monkeypatch):
    # Given an operator whose password is wrong.
    setup = member_auth_setup(monkeypatch)
    setup.identity.is_platform_admin = True
    monkeypatch.setattr(auth, "verify_password_async", AsyncMock(return_value=False))
    # When member login is attempted.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/login",
            json={"login_identifier": "member@example.com", "password": "wrong"},
        )
    # Then it does not disclose the audience until credentials are valid.
    assert response.status_code == 401
    setup.mint.assert_not_awaited()


async def test_shared_login_still_accepts_operator_and_broad_projection(monkeypatch):
    # Given a platform identity with a member membership.
    setup = member_auth_setup(monkeypatch)
    setup.identity.is_platform_admin = True
    # When the original shared route is used with a member Origin.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/auth/login",
            json={"login_identifier": "member@example.com", "password": "password"},
            headers={"Origin": "https://members.example.com"},
        )
    # Then the original broad contract and admin access remain available.
    assert response.status_code == 200
    assert response.json()["user"]["is_platform_admin"] is True
    assert response.json()["identity"]["is_platform_admin"] is True


async def test_member_login_preserves_pending_company_flow(monkeypatch):
    # Given an eligible tenantless account.
    setup = member_auth_setup(monkeypatch)
    setup.users[0].tenant_id = None
    monkeypatch.setattr(
        "app.services.org_membership.lookup_tenant_by_email_domain",
        AsyncMock(return_value=setup.tenants[0]),
    )
    # When it signs in.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/login",
            json={"login_identifier": "member@example.com", "password": "password"},
        )
    # Then no company is silently attached and the suggestion survives projection.
    assert response.status_code == 200
    assert response.json()["user"]["tenant_id"] is None
    assert response.json()["needs_org_confirm"] is True
    assert response.json()["suggested_org"]["id"] == str(setup.tenants[0].id)


async def test_member_login_keeps_multitenant_selection_without_minting(monkeypatch):
    # Given two eligible company memberships.
    setup = member_auth_setup(monkeypatch, count=2)
    # When no company was selected.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/login",
            json={"login_identifier": "member@example.com", "password": "password"},
        )
    # Then choices are preserved, without issuing a session yet.
    assert response.status_code == 200
    assert response.json()["requires_tenant_selection"] is True
    assert len(response.json()["tenants"]) == 2
    setup.mint.assert_not_awaited()
