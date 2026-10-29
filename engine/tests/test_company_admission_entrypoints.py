from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.api import auth
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.services.auth_provider import ExternalUserInfo, GoogleAuthProvider
from company_admission_fixtures import AdmissionScenario, admission_http_domain, admission_scenario

__all__ = ["admission_http_domain", "admission_scenario"]


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_domain_signup_is_initial_admission_without_transfer(
    admission_scenario: AdmissionScenario, admission_http_domain: str, monkeypatch: pytest.MonkeyPatch, decision: str
):
    # Given a new verified email in the company's claimed domain.
    scenario = admission_scenario
    monkeypatch.setattr(auth, "_send_verification_email_task", AsyncMock())
    registration = await scenario.client.post(
        "/api/auth/register/init", json={"email": f"new-domain@{admission_http_domain}", "password": "qa-password"}
    )
    assert registration.status_code == 201, registration.text
    body = registration.json()
    assert body["user"]["tenant_id"] is None
    assert body["needs_org_confirm"] is True
    admission = body["admission_request"]
    assert admission["method"] == "verified_domain"
    assert admission["source_tenant_id"] is None
    assert admission["transfer"] is None
    # When the target genesis administrator decides the initial request.
    response = await scenario.client.post(
        f"/api/company-admissions/{admission['id']}/decision",
        json={"decision": decision},
        headers=scenario.headers(scenario.genesis),
    )
    # Then only approval attaches the same user with destination quotas, without a transfer.
    assert response.status_code == 200, response.text
    user = await user_dao.get(UUID(body["user"]["id"]))
    assert user is not None
    assert str(user.id) == body["user"]["id"]
    assert user.tenant_id == (scenario.target_id if decision == "approved" else None)
    if decision == "approved":
        assert user.quota_max_agents == 9
    assert await scenario.db.fetchval("SELECT count(*) FROM company_transfer_requests") == 0


@pytest.mark.parametrize("invalidated", ["verification", "domain"])
async def test_initial_domain_approval_revalidates_email_proof(
    admission_scenario: AdmissionScenario, admission_http_domain: str, invalidated: str
):
    # Given a pending initial domain request whose ownership proof is subsequently invalidated.
    scenario = admission_scenario
    response = await scenario.client.post(
        "/api/auth/register/init", json={"email": f"invalidated@{admission_http_domain}", "password": "qa-password"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    if invalidated == "verification":
        await scenario.db.execute(
            "UPDATE identities SET email_verified=false WHERE email=%(email)s",
            {"email": f"invalidated@{admission_http_domain}"},
        )
    else:
        await scenario.db.execute(
            "DELETE FROM tenant_email_domains WHERE domain=%(domain)s", {"domain": admission_http_domain}
        )
    # When the genesis administrator attempts approval.
    decision = await scenario.client.post(
        f"/api/company-admissions/{body['admission_request']['id']}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    # Then invalid proof prevents assignment, with no transfer created.
    assert decision.status_code == 403, decision.text
    user = await user_dao.get(UUID(body["user"]["id"]))
    assert user is not None
    assert user.tenant_id is None
    assert await scenario.db.fetchval("SELECT count(*) FROM company_transfer_requests") == 0


@pytest.mark.parametrize("source", ["company", "kabers", "unassigned"])
@pytest.mark.parametrize("method", ["invitation", "domain"])
async def test_company_join_is_for_unassigned_newcomers_only(
    admission_scenario: AdmissionScenario, source: str, method: str
):
    scenario = admission_scenario
    if source == "company":
        await scenario.db.execute(
            "UPDATE tenants SET is_default_end_user_org=false WHERE id=%(id)s", {"id": scenario.source_id}
        )
    elif source == "unassigned":
        await scenario.db.execute("UPDATE users SET tenant_id=NULL WHERE id=%(id)s", {"id": scenario.applicant.id})
    path = "/api/tenants/join" if method == "invitation" else "/api/tenants/join-suggested"
    body = {"invitation_code": "JOIN"} if method == "invitation" else {"tenant_id": str(scenario.target_id)}

    response = await scenario.client.post(path, json=body, headers=scenario.headers(scenario.applicant))

    # Members, whatever their organization, must transfer with a password; a newcomer's invitation admits at once
    # while a verified domain still waits for the genesis administrator.
    newcomer = source == "unassigned"
    admitted = newcomer and method == "invitation"
    expected = (200 if admitted else 202) if newcomer else (409 if method == "invitation" else 403)
    assert response.status_code == expected, response.text
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == int(newcomer)
    assert (await scenario.current_applicant()).tenant_id == (
        scenario.target_id if admitted else None if newcomer else scenario.source_id
    )
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == int(admitted)


@pytest.mark.parametrize("method", ["domain", "invitation"])
@pytest.mark.parametrize("password", ["wrong-password", "qa-password"])
async def test_company_transfer_requires_password_before_admission(
    admission_scenario: AdmissionScenario, method: str, password: str
):
    scenario = admission_scenario
    await scenario.db.execute(
        "UPDATE tenants SET is_default_end_user_org=false WHERE id=%(id)s", {"id": scenario.source_id}
    )
    body = {"password": password}
    body.update({"invitation_code": "JOIN"} if method == "invitation" else {"tenant_id": str(scenario.target_id)})

    response = await scenario.client.post(
        "/api/tenants/transfer", json=body, headers=scenario.headers(scenario.applicant)
    )

    assert response.status_code == (409 if password == "qa-password" else 401), response.text
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0


@pytest.mark.parametrize("path", ["/api/tenants/join-suggested", "/api/tenants/transfer"])
async def test_default_member_cannot_reach_a_company_through_a_verified_domain(
    admission_scenario: AdmissionScenario, path: str
):
    scenario = admission_scenario
    body = {"tenant_id": str(scenario.target_id), "password": "qa-password"}

    response = await scenario.client.post(path, json=body, headers=scenario.headers(scenario.applicant))

    assert response.status_code == 403, response.text
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


async def test_invitation_registration_remains_kabers(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = admission_scenario
    monkeypatch.setattr(auth, "_send_verification_email_task", AsyncMock())
    await scenario.db.execute(
        "UPDATE invitation_codes SET invited_email=%(email)s WHERE code='JOIN'",
        {"email": "new@company.example.com"},
    )
    response = await scenario.client.post(
        "/api/auth/register/init",
        json={
            "email": "new@company.example.com",
            "password": "qa-password",
            "display_name": "New Applicant",
            "invitation_code": "JOIN",
            "target_tenant_id": str(scenario.target_id),
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["user"]["tenant_id"] == str(scenario.source_id)
    assert payload["admission_request"]["status"] == "pending"
    assert payload["admission_request"]["target_tenant"]["id"] == str(scenario.target_id)
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0


@pytest.mark.parametrize("explicit_tenant", [True, False])
async def test_fresh_sso_creates_only_kabers_membership(admission_scenario: AdmissionScenario, explicit_tenant: bool):
    scenario = admission_scenario
    provider = await identity_provider_dao.create(
        obj_in={
            "provider_type": "google",
            "name": "Google",
            "tenant_id": None if explicit_tenant else scenario.target_id,
        }
    )
    adapter = GoogleAuthProvider(provider)
    info = ExternalUserInfo(
        provider_type="google", provider_user_id="fresh-sso", name="SSO Applicant", email="sso@company.example.com"
    )
    user, created = await adapter.find_or_create_user(info, str(scenario.target_id) if explicit_tenant else None)
    repeated, _ = await adapter.find_or_create_user(info, str(scenario.target_id) if explicit_tenant else None)
    assert created is True
    assert user.tenant_id == scenario.source_id
    assert repeated.id == user.id
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 1
    assert (
        await scenario.db.fetchval(
            "SELECT count(*) FROM org_members WHERE tenant_id=%(id)s AND user_id=%(user)s",
            {"id": scenario.target_id, "user": user.id},
        )
        == 0
    )


async def test_admitted_sso_does_not_request_again(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    await user_dao.update(db_obj=scenario.applicant, obj_in={"tenant_id": scenario.target_id})
    provider = await identity_provider_dao.create(
        obj_in={"provider_type": "google", "name": "Google", "tenant_id": scenario.target_id}
    )
    user, _ = await GoogleAuthProvider(provider).find_or_create_user(
        ExternalUserInfo(
            provider_type="google",
            provider_user_id="admitted-sso",
            email="applicant@acme.test",
            name="Applicant",
        )
    )
    assert user.tenant_id == scenario.target_id
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0


async def test_other_pending_target_conflicts(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    await scenario.submit()
    other = await tenant_dao.create(obj_in={"name": "Other", "slug": "other"})
    await scenario.db.execute(
        "INSERT INTO invitation_codes (id,code,tenant_id,max_uses,used_count,invited_email) "
        "VALUES (%(id)s,'OTHER',%(target)s,1,0,'applicant@acme.test')",
        {"id": uuid4(), "target": other.id},
    )
    response = await scenario.client.post(
        "/api/tenants/transfer",
        json={"invitation_code": "OTHER", "password": "qa-password"},
        headers=scenario.headers(scenario.applicant),
    )
    assert response.status_code == 409
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 1
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='OTHER'") == 0


async def test_admin_assignment_cannot_move_company(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    response = await scenario.client.put(
        f"/api/tenants/{scenario.target_id}/assign-user/{scenario.applicant.id}",
        json={"role": "member"},
        headers=scenario.headers(scenario.outsider),
    )
    assert response.status_code == 409, response.text
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
