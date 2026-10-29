from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.api import auth
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.user_dao import user_dao
from app.services.auth_provider import ExternalUserInfo, GoogleAuthProvider
from app.services.auth_registry import auth_provider_registry
from app.services.email_verification_service import email_verification_service
from company_admission_fixtures import AdmissionScenario, admission_http_domain, admission_scenario

__all__ = ["admission_http_domain", "admission_scenario"]


@pytest.fixture
def smtp_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import registration_service, system_email_service
    from app.services.system_email_service import SystemEmailConfig

    config = SystemEmailConfig(
        smtp_host="smtp.test",
        smtp_port=587,
        smtp_username="sender@test.example",
        smtp_password="secret",
        from_address="sender@test.example",
        from_name="QA",
        smtp_ssl=False,
        smtp_timeout_seconds=10,
    )
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=config))
    monkeypatch.setattr(registration_service, "resolve_email_config_async", AsyncMock(return_value=config))


@pytest.mark.parametrize("conflicting_target", [False, True])
@pytest.mark.usefixtures("smtp_enabled")
async def test_domain_sso_waits_for_email_proof_and_initial_approval(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, conflicting_target: bool
):
    # Given a domain-matched fresh SSO identity, optionally targeting a different provider company.
    scenario = admission_scenario
    target_id = scenario.source_id if conflicting_target else scenario.target_id
    provider = await identity_provider_dao.create(
        obj_in={"provider_type": "google", "name": "Google", "tenant_id": target_id}
    )
    adapter = GoogleAuthProvider(provider)
    info = ExternalUserInfo(
        provider_type="google", provider_user_id="domain-initial-sso", email="initial-sso@acme.test"
    )
    user, created = await adapter.find_or_create_user(info)
    repeated, repeated_created = await adapter.find_or_create_user(info)
    assert created is True
    assert repeated.id == user.id
    assert repeated_created is False
    assert user.tenant_id is None
    assert user.email_verified is False
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    monkeypatch.setattr(
        email_verification_service,
        "consume_email_verification_token",
        AsyncMock(return_value={"identity_id": user.identity_id}),
    )
    verification = await scenario.client.post("/api/auth/verify-email", json={"token": "owned-email-token"})
    assert verification.status_code == 200, verification.text
    row = await scenario.db.fetchone("SELECT * FROM company_admission_requests")
    assert row is not None
    assert row["method"] == "verified_domain"
    assert row["target_tenant_id"] == scenario.target_id
    assert row["source_tenant_id"] is None
    # When the target genesis administrator approves after verification.
    response = await scenario.client.post(
        f"/api/company-admissions/{row['id']}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    # Then repeated SSO resolves the same admitted user without another request or transfer.
    assert response.status_code == 200, response.text
    admitted, created = await adapter.find_or_create_user(info)
    assert admitted.id == user.id
    assert admitted.tenant_id == scenario.target_id
    assert created is False
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM company_transfer_requests") == 0


@pytest.mark.parametrize("global_provider", [True, False])
async def test_sso_identity_survives_approval(admission_scenario: AdmissionScenario, global_provider: bool):
    scenario = admission_scenario
    provider = await identity_provider_dao.create(
        obj_in={
            "provider_type": "google",
            "name": "Google",
            "tenant_id": None if global_provider else scenario.target_id,
        }
    )
    adapter = GoogleAuthProvider(provider)
    info = ExternalUserInfo(provider_type="google", provider_user_id="stable-external-id", name="SSO Applicant")
    selected = str(scenario.target_id) if global_provider else None
    user, _ = await adapter.find_or_create_user(info, selected)
    request_id = await scenario.db.fetchval("SELECT id FROM company_admission_requests")
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    assert response.status_code == 200, response.text
    admitted, created = await adapter.find_or_create_user(info, selected)
    assert admitted.id == user.id
    assert admitted.tenant_id == scenario.target_id
    assert created is False
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 1


@pytest.mark.parametrize("global_provider", [True, False])
@pytest.mark.usefixtures("smtp_enabled")
async def test_smtp_first_sso_returns_active_kabers_user(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, global_provider: bool
):
    scenario = admission_scenario
    provider = await identity_provider_dao.create(
        obj_in={
            "provider_type": "google",
            "name": "Google",
            "tenant_id": None if global_provider else scenario.target_id,
        }
    )
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=GoogleAuthProvider(provider)))
    pending = auth.OAuthPendingCache(
        provider_type="google",
        token_data={},
        allowed_tenant_ids=[scenario.target_id],
        user_info=auth.OAuthPendingUserInfo(
            provider_type="google",
            provider_user_id="selected-company",
            name="Applicant",
            email="sso@company.example.com",
            raw_data={},
        ),
    )
    monkeypatch.setattr(auth, "_get_oauth_pending", AsyncMock(return_value=pending))
    response = await scenario.client.post(
        "/api/auth/google/callback",
        json={"pending_token": "opaque-cache-key", "tenant_id": str(scenario.target_id), "state": ""},
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["tenant_id"] == str(scenario.source_id)
    assert response.json()["user"]["is_active"] is True
    assert response.json()["user"]["email_verified"] is False
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"
    mine = await scenario.client.get(
        "/api/company-admissions/mine", headers={"Authorization": f"Bearer {response.json()['access_token']}"}
    )
    assert mine.status_code == 200
    assert mine.json()[0]["target_tenant"]["id"] == str(scenario.target_id)


@pytest.mark.parametrize("disabled", ["membership", "identity"])
@pytest.mark.usefixtures("smtp_enabled")
async def test_smtp_sso_preserves_existing_account_disablement(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, disabled: str
):
    scenario = admission_scenario
    if disabled == "membership":
        await scenario.db.execute("UPDATE users SET is_active=false WHERE id=%(id)s", {"id": scenario.applicant.id})
    else:
        await scenario.db.execute(
            "UPDATE identities SET is_active=false WHERE id=%(id)s", {"id": scenario.applicant.identity_id}
        )
    provider = await identity_provider_dao.create(obj_in={"provider_type": "google", "name": "Google"})
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=GoogleAuthProvider(provider)))
    pending = auth.OAuthPendingCache(
        provider_type="google",
        token_data={},
        allowed_tenant_ids=[scenario.target_id],
        user_info=auth.OAuthPendingUserInfo(
            provider_type="google", provider_user_id="existing-disabled", email="applicant@acme.test", raw_data={}
        ),
    )
    monkeypatch.setattr(auth, "_get_oauth_pending", AsyncMock(return_value=pending))

    response = await scenario.client.post(
        "/api/auth/google/callback",
        json={"pending_token": "opaque-cache-key", "tenant_id": str(scenario.target_id), "state": ""},
    )

    assert response.status_code == 403, response.text
    assert (await scenario.current_applicant()).is_active is (disabled != "membership")
    assert await scenario.db.fetchval(
        "SELECT is_active FROM identities WHERE id=%(id)s", {"id": scenario.applicant.identity_id}
    ) is (disabled != "identity")
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0


@pytest.mark.usefixtures("smtp_enabled")
async def test_smtp_registration_can_request_before_email_verification(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = admission_scenario
    monkeypatch.setattr(auth, "_send_verification_email_task", AsyncMock())
    await scenario.db.execute(
        "UPDATE invitation_codes SET invited_email=%(email)s WHERE code='JOIN'",
        {"email": "unverified@company.example.com"},
    )
    response = await scenario.client.post(
        "/api/auth/register/init",
        json={
            "email": "unverified@company.example.com",
            "password": "qa-password",
            "invitation_code": "JOIN",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["admission_request"]["status"] == "pending"
    assert response.json()["user"]["tenant_id"] == str(scenario.source_id)
    assert response.json()["user"]["is_active"] is False


@pytest.mark.usefixtures("smtp_enabled")
async def test_smtp_domain_signup_verification_stages_initial_admission_before_approval(
    admission_scenario: AdmissionScenario, admission_http_domain: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an SMTP-configured signup with a claimed domain and no invitation.
    scenario = admission_scenario
    email = f"smtp-signup@{admission_http_domain}"
    monkeypatch.setattr(auth, "_send_verification_email_task", AsyncMock())
    registration = await scenario.client.post(
        "/api/auth/register/init", json={"email": email, "password": "qa-password"}
    )
    assert registration.status_code == 201, registration.text
    body = registration.json()
    user_id = UUID(body["user"]["id"])
    user = await user_dao.get_with_identity(user_id, fresh=True)
    assert user is not None
    assert user.tenant_id is None
    assert (user.email_verified, user.is_active) == (False, False)
    assert body["admission_request"] is None
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    monkeypatch.setattr(
        email_verification_service,
        "consume_email_verification_token",
        AsyncMock(return_value={"identity_id": str(user.identity_id), "email": email}),
    )
    # When email ownership is verified through HTTP and the genesis administrator approves.
    verification = await scenario.client.post("/api/auth/verify-email", json={"token": "owned-email-token"})
    assert verification.status_code == 200, verification.text
    assert verification.json()["user"]["tenant_id"] is None
    assert verification.json()["identity"]["email_verified"] is True
    assert (
        await scenario.db.fetchval("SELECT email_verified FROM identities WHERE id=%(id)s", {"id": user.identity_id})
        is True
    )
    row = await scenario.db.fetchone("SELECT * FROM company_admission_requests")
    assert row is not None
    assert (row["status"], row["method"], row["source_tenant_id"]) == ("pending", "verified_domain", None)
    assert row["applicant_user_id"] == user_id
    assert row["target_tenant_id"] == scenario.target_id
    response = await scenario.client.post(
        f"/api/company-admissions/{row['id']}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    # Then the same user receives the target company and quotas without a transfer.
    assert response.status_code == 200, response.text
    admitted = await user_dao.get(user_id)
    assert admitted is not None
    assert (admitted.tenant_id, admitted.quota_max_agents) == (scenario.target_id, 9)
    assert await scenario.db.fetchval("SELECT count(*) FROM company_transfer_requests") == 0


async def test_kabers_join_remains_immediate(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    await scenario.db.execute("UPDATE users SET tenant_id=NULL WHERE id=%(id)s", {"id": scenario.applicant.id})
    response = await scenario.client.post("/api/tenants/join-default", headers=scenario.headers(scenario.applicant))
    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["id"] == str(scenario.source_id)
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
