from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from tests.member_auth_fixtures import member_auth_setup, oauth_setup

from app.core.domain.users import UserRole
from app.services.registration_service import registration_service
from app.services.sso_service import sso_service


@pytest.mark.parametrize("step", ["code", "pending"])
@pytest.mark.parametrize("operator", ["identity", "role"])
async def test_member_oauth_rejects_operator_before_link_cache_or_session(monkeypatch, step, operator):
    # Given an operator resolved by the real provider account flow.
    setup = member_auth_setup(monkeypatch, count=2)
    setup.identity.is_platform_admin = operator == "identity"
    if operator == "role":
        setup.users[-1].role = UserRole.PLATFORM_ADMIN
    stage, link = oauth_setup(monkeypatch, setup)
    payload = (
        {"state": "state", "code": "code"}
        if step == "code"
        else {
            "state": "state",
            "pending_token": "pending",
            "tenant_id": str(setup.tenants[0].id),
        }
    )
    # When either OAuth step is called through the member audience.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/feishu/callback", json=payload)
    # Then neither admissions, linking, pending credentials nor sessions are issued.
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "member_audience_rejected"
    stage.assert_not_awaited()
    link.assert_not_awaited()
    setup.pending.assert_not_awaited()
    setup.mint.assert_not_awaited()


@pytest.mark.parametrize("existing_user", [False, True])
@pytest.mark.parametrize("operator", ["identity", "role"])
async def test_member_oauth_checks_discovered_identity_before_registration_side_effects(
    monkeypatch,
    existing_user,
    operator,
):
    # Given a provider account whose global identity is discovered during registration.
    setup = member_auth_setup(monkeypatch, count=2)
    setup.identity.is_platform_admin = operator == "identity"
    if operator == "role":
        setup.users[-1].role = UserRole.PLATFORM_ADMIN
    setup.users[0].identity_id = None
    setup.users[0].identity = None
    stage, link = oauth_setup(monkeypatch, setup)
    if not existing_user:
        monkeypatch.setattr(sso_service, "resolve_user_identity", AsyncMock(return_value=None))
        monkeypatch.setattr(sso_service, "match_user_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(registration_service, "find_or_create_identity", AsyncMock(return_value=setup.identity))
    create_user = AsyncMock()
    monkeypatch.setattr("app.services.sso_company_admission.create_sso_user", create_user)
    # When OAuth registration identifies the platform account.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/feishu/callback", json={"state": "state", "code": "code"})
    # Then admission, account creation, identity links and session issuance have not started.
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "member_audience_rejected"
    stage.assert_not_awaited()
    create_user.assert_not_awaited()
    link.assert_not_awaited()
    setup.mint.assert_not_awaited()


@pytest.mark.parametrize("step", ["code", "pending"])
async def test_member_oauth_projects_records_for_both_steps(monkeypatch, step):
    # Given a resolved eligible member account.
    setup = member_auth_setup(monkeypatch)
    oauth_setup(monkeypatch, setup)
    payload = (
        {"state": "state", "code": "code"}
        if step == "code"
        else {
            "state": "state",
            "pending_token": "pending",
            "tenant_id": str(setup.tenants[0].id),
        }
    )
    # When the provider callback succeeds.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/feishu/callback", json=payload)
    # Then the session carries a member-only projection.
    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(setup.users[0].id)
    assert "is_platform_admin" not in response.json()["user"]
    setup.mint.assert_awaited_once()


async def test_member_oauth_preserves_eligible_pending_tenant_selection(monkeypatch):
    # Given two eligible memberships.
    setup = member_auth_setup(monkeypatch, count=2)
    oauth_setup(monkeypatch, setup)
    # When the initial OAuth callback resolves the account.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/feishu/callback", json={"state": "state", "code": "code"})
    # Then selection is cached without minting a session.
    assert response.status_code == 200
    assert response.json()["requires_tenant_selection"] is True
    assert len(response.json()["tenants"]) == 2
    assert response.json()["pending_token"]
    setup.pending.assert_awaited_once()
    setup.mint.assert_not_awaited()
