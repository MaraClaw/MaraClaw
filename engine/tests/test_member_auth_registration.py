from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from tests.member_auth_fixtures import member_auth_setup

from app.api import auth
from app.core.domain.users import UserRole
from app.services import org_membership, system_email_service
from app.services.email_verification_service import email_verification_service
from app.services.org_membership import NewUserPlacement
from app.services.registration_service import registration_service


@pytest.mark.parametrize("operator", ["identity", "role"])
@pytest.mark.parametrize("race", [False, True])
async def test_member_registration_rejects_known_operator_before_placement(monkeypatch, operator, race):
    # Given an operator identity found initially or by the transactional registration lookup.
    setup = member_auth_setup(monkeypatch)
    setup.identity.is_platform_admin = operator == "identity"
    if operator == "role":
        setup.users[0].role = UserRole.PLATFORM_ADMIN
    if race:
        monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=None))
    monkeypatch.setattr(auth, "hash_password_async", AsyncMock(return_value="computed-hash"))
    monkeypatch.setattr(registration_service, "find_or_create_identity", AsyncMock(return_value=setup.identity))
    placement = AsyncMock()
    admission = AsyncMock()
    create_user = AsyncMock()
    monkeypatch.setattr(org_membership, "place_new_registration", placement)
    monkeypatch.setattr(registration_service, "create_user_with_identity", create_user)
    monkeypatch.setattr("app.services.domain_admission_auto.stage_verified_domain_admission", admission)
    # When member registration authenticates the existing account.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/register/init",
            json={"email": "member@example.com", "password": "password"},
        )
    # Then no placement, user creation, admission or session side effects occur.
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "member_audience_rejected"
    placement.assert_not_awaited()
    create_user.assert_not_awaited()
    admission.assert_not_awaited()
    setup.mint.assert_not_awaited()


@pytest.mark.parametrize("verified", [False, True])
async def test_member_registration_preserves_new_tenantless_account_flow(monkeypatch, verified):
    # Given a new eligible identity, with the existing verification and suggested-company policy.
    setup = member_auth_setup(monkeypatch)
    setup.users[0].tenant_id = None
    setup.identity.email_verified = verified
    monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(auth.user_dao, "get_by_identity_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=None))
    monkeypatch.setattr(auth, "hash_password_async", AsyncMock(return_value="computed-hash"))
    monkeypatch.setattr(registration_service, "find_or_create_identity", AsyncMock(return_value=setup.identity))
    monkeypatch.setattr(registration_service, "create_user_with_identity", AsyncMock(return_value=setup.users[0]))
    monkeypatch.setattr(
        org_membership,
        "place_new_registration",
        AsyncMock(
            return_value=NewUserPlacement(
                tenant_id=None,
                suggested=setup.tenants[0],
                needs_org_confirm=True,
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.domain_admission_auto.stage_verified_domain_admission", AsyncMock(return_value=None)
    )
    email = AsyncMock()
    monkeypatch.setattr(auth, "_send_verification_email_task", email)
    # When member registration succeeds.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/register/init",
            json={"email": "member@example.com", "password": "password"},
        )
    # Then no company is assigned by projection and verification behavior is preserved.
    assert response.status_code == 201
    body = response.json()
    assert body["needs_org_confirm"] is True
    assert body["suggested_org"]["id"] == str(setup.tenants[0].id)
    assert body["user"]["tenant_id"] is None
    assert body["user"]["email_verified"] is verified
    assert "is_platform_admin" not in body["user"]
    assert email.await_count == int(not verified)
    setup.mint.assert_awaited_once()


@pytest.mark.parametrize("operator", ["identity", "role"])
async def test_member_verification_rejects_operator_before_activation_or_admission(monkeypatch, operator):
    # Given a valid verification token for a platform operator.
    setup = member_auth_setup(monkeypatch)
    setup.identity.is_platform_admin = operator == "identity"
    if operator == "role":
        setup.users[0].role = UserRole.PLATFORM_ADMIN
    monkeypatch.setattr(
        email_verification_service,
        "consume_email_verification_token",
        AsyncMock(
            return_value={
                "identity_id": setup.identity.id,
            }
        ),
    )
    activate = AsyncMock()
    admission = AsyncMock()
    monkeypatch.setattr(auth.identity_dao, "update", activate)
    monkeypatch.setattr("app.services.domain_admission_auto.stage_verified_domain_admission", admission)
    # When the member verification route consumes the token.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/verify-email", json={"token": "verify-token"})
    # Then the operator receives no activation, admission or session.
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "member_audience_rejected"
    activate.assert_not_awaited()
    admission.assert_not_awaited()
    setup.mint.assert_not_awaited()


@pytest.mark.parametrize("has_user", [False, True])
async def test_member_verification_preserves_identity_only_and_member_projections(monkeypatch, has_user):
    # Given an eligible identity and an optional representative membership.
    setup = member_auth_setup(monkeypatch)
    setup.identity.must_change_password = True
    monkeypatch.setattr(
        email_verification_service,
        "consume_email_verification_token",
        AsyncMock(
            return_value={
                "identity_id": setup.identity.id,
            }
        ),
    )
    monkeypatch.setattr(auth.identity_dao, "update", AsyncMock(return_value=setup.identity))
    monkeypatch.setattr(auth.user_dao, "update", AsyncMock(return_value=setup.users[0]))
    if not has_user:
        monkeypatch.setattr(auth.user_dao, "get_by_identity_id", AsyncMock(return_value=[]))
        monkeypatch.setattr(auth.user_dao, "get_representative_user_for_identity", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.domain_admission_auto.stage_verified_domain_admission", AsyncMock(return_value=None)
    )
    # When member verification succeeds.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/verify-email", json={"token": "verify-token"})
    # Then identity-only sessions and nested force-change state remain representable without operator fields.
    assert response.status_code == 200
    body = response.json()
    assert body["identity"]["must_change_password"] is True
    assert "is_platform_admin" not in body["identity"]
    if has_user:
        assert body["user"]["must_change_password"] is True
        assert "is_platform_admin" not in body["user"]
    else:
        assert body["user"] is None
        assert body["needs_org_confirm"] is True
    setup.mint.assert_awaited_once()
