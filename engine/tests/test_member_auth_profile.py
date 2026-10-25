from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from tests.member_auth_fixtures import member_auth_setup, oauth_setup

from app.api import auth
from app.core import security
from app.core.domain.users import UserRole
from app.services.sso_service import sso_service


@pytest.mark.parametrize("operator", ["identity", "role"])
@pytest.mark.parametrize("operation", ["get", "patch", "bind", "unbind"])
async def test_member_profile_rejects_operator_on_all_record_routes(monkeypatch, operator, operation):
    # Given a valid shared JWT for an operator, even with an ordinary member role.
    setup = member_auth_setup(monkeypatch)
    setup.identity.is_platform_admin = operator == "identity"
    if operator == "role":
        setup.users[0].role = UserRole.PLATFORM_ADMIN
    write = AsyncMock()
    link = AsyncMock()
    unlink = AsyncMock()
    monkeypatch.setattr(auth.user_dao, "update", write)
    monkeypatch.setattr(sso_service, "link_identity", link)
    monkeypatch.setattr(sso_service, "unlink_identity", unlink)
    token = await security.create_access_token(
        str(setup.users[0].id), setup.users[0].role, identity_id=setup.identity.id
    )
    method = "GET" if operation == "get" else "PATCH" if operation == "patch" else "POST"
    path = "/api/member/auth/me" if operation in {"get", "patch"} else f"/api/member/auth/feishu/{operation}"
    # When any member profile or identity mutation is requested.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "display_name": "Changed",
                "provider_type": "feishu",
                "code": "code",
            },
        )
    # Then shared credentials cannot cross the member boundary or mutate the account.
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "member_audience_rejected"
    write.assert_not_awaited()
    link.assert_not_awaited()
    unlink.assert_not_awaited()


@pytest.mark.parametrize("operation", ["get", "patch", "bind", "unbind"])
async def test_member_profile_projects_records_for_eligible_account(monkeypatch, operation):
    # Given an authenticated eligible member and a real provider account flow.
    setup = member_auth_setup(monkeypatch)
    oauth_setup(monkeypatch, setup)
    monkeypatch.setattr(sso_service, "check_duplicate_identity", AsyncMock(return_value=None))
    monkeypatch.setattr(sso_service, "unlink_identity", AsyncMock(return_value=True))
    token = await security.create_access_token(
        str(setup.users[0].id), setup.users[0].role, identity_id=setup.identity.id
    )
    method = "GET" if operation == "get" else "PATCH" if operation == "patch" else "POST"
    path = "/api/member/auth/me" if operation in {"get", "patch"} else f"/api/member/auth/feishu/{operation}"
    # When a record-returning member route succeeds.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "provider_type": "feishu",
                "code": "code",
            },
        )
    # Then the record is projected through the member DTO.
    assert response.status_code == 200
    assert response.json()["id"] == str(setup.users[0].id)
    assert "is_platform_admin" not in response.json()


async def test_member_me_preserves_force_change(monkeypatch):
    # Given a force-change eligible account using the existing JWT generation protocol.
    setup = member_auth_setup(monkeypatch)
    setup.identity.must_change_password = True
    token = await security.create_access_token(
        str(setup.users[0].id), setup.users[0].role, identity_id=setup.identity.id
    )
    # When its member session is refreshed.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.get("/api/member/auth/me", headers={"Authorization": f"Bearer {token}"})
    # Then force-change is visible without the platform flag.
    assert response.status_code == 200
    assert response.json()["must_change_password"] is True
    assert "is_platform_admin" not in response.json()


async def test_shared_me_preserves_operator_fields(monkeypatch):
    # Given a platform identity with a valid shared credential.
    setup = member_auth_setup(monkeypatch)
    setup.identity.is_platform_admin = True
    token = await security.create_access_token(
        str(setup.users[0].id), setup.users[0].role, identity_id=setup.identity.id
    )
    # When it uses the original shared profile route.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    # Then the original operator-sensitive JSON remains unchanged.
    assert response.status_code == 200
    assert response.json()["is_platform_admin"] is True
