from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api import auth
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.services.auth_provider import ExternalUserInfo, GoogleAuthProvider
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]


@pytest.mark.parametrize("source", ["company", "openclaw", "unassigned"])
@pytest.mark.parametrize("method", ["invitation", "domain"])
async def test_company_join_requires_transfer_from_company_source(
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

    assert response.status_code == (409 if source == "company" else 202), response.text
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == (source != "company")
    assert (await scenario.current_applicant()).tenant_id == (None if source == "unassigned" else scenario.source_id)
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0


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


@pytest.mark.parametrize("method", ["domain_join", "domain_transfer", "invitation_transfer"])
async def test_company_entrypoints_create_pending_without_moving(admission_scenario: AdmissionScenario, method: str):
    scenario = admission_scenario
    body = {"tenant_id": str(scenario.target_id)}
    path = "/api/tenants/join-suggested"
    if method != "domain_join":
        path = "/api/tenants/transfer"
        body["password"] = "qa-password"
    if method == "invitation_transfer":
        body = {"password": "qa-password", "invitation_code": "JOIN"}
    response = await scenario.client.post(path, json=body, headers=scenario.headers(scenario.applicant))
    assert response.status_code == 202, response.text
    expected_method = "invitation" if method == "invitation_transfer" else "verified_domain"
    assert response.json()["request"]["method"] == expected_method
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


async def test_invitation_registration_remains_openclaw(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = admission_scenario
    monkeypatch.setattr(auth, "_send_verification_email_task", AsyncMock())
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
async def test_fresh_sso_creates_only_openclaw_membership(admission_scenario: AdmissionScenario, explicit_tenant: bool):
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
        "INSERT INTO invitation_codes (id,code,tenant_id,max_uses,used_count) VALUES (%(id)s,'OTHER',%(target)s,1,0)",
        {"id": uuid4(), "target": other.id},
    )
    response = await scenario.client.post(
        "/api/tenants/join", json={"invitation_code": "OTHER"}, headers=scenario.headers(scenario.applicant)
    )
    assert response.status_code == 409
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 1


async def test_admin_assignment_cannot_move_company(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    response = await scenario.client.put(
        f"/api/tenants/{scenario.target_id}/assign-user/{scenario.applicant.id}",
        json={"role": "member"},
        headers=scenario.headers(scenario.outsider),
    )
    assert response.status_code == 409, response.text
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
