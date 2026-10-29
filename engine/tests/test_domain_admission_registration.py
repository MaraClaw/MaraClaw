from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dao.invitation_code_dao import invitation_code_dao
from app.records.company_admission import AdmissionMethod, AdmissionStatus
from app.records.identity import IdentityProviderRecord
from app.records.invitation import InvitationCodeRecord
from app.services.sso_company_admission import stage_sso_admission
from domain_admission_fixture import DomainAdmissionFixture, domain_admission_fixture

__all__ = ["domain_admission_fixture"]


@pytest.mark.parametrize("approved_initial", [False, True])
async def test_sso_replay_exemption_requires_approved_initial_domain_admission(
    domain_admission_fixture: DomainAdmissionFixture, monkeypatch: pytest.MonkeyPatch, approved_initial: bool
) -> None:
    # Given a linked company member, with or without an approved initial domain admission.
    fixture = domain_admission_fixture
    fixture.applicant.tenant_id = fixture.target.id
    provider = IdentityProviderRecord(id=uuid4(), provider_type="google", name="Google", tenant_id=fixture.other.id)
    monkeypatch.setattr(
        "app.services.sso_company_admission.find_pending_sso_user", AsyncMock(return_value=fixture.applicant)
    )
    monkeypatch.setattr(
        "app.services.sso_company_admission.company_admission_dao.has_approved_initial_domain_admission",
        AsyncMock(return_value=approved_initial),
    )
    submit = AsyncMock(side_effect=HTTPException(409, "Company members must transfer"))
    monkeypatch.setattr("app.services.sso_company_admission.submit_request", submit)
    # When SSO targets a different company, only the initially admitted account reuses its membership.
    if approved_initial:
        assert await stage_sso_admission(fixture.applicant, provider, "external", target_id=fixture.other.id)
        submit.assert_not_awaited()
    else:
        with pytest.raises(HTTPException) as error:
            await stage_sso_admission(fixture.applicant, provider, "external", target_id=fixture.other.id)
        assert error.value.status_code == 409


@pytest.mark.parametrize("verified", [False, True])
async def test_tenantless_sso_keeps_domain_proof_and_provider_linkage(
    domain_admission_fixture: DomainAdmissionFixture,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
) -> None:
    # Given a tenantless domain applicant and a provider targeting another company.
    fixture = domain_admission_fixture
    fixture.applicant.tenant_id = None
    fixture.identity.email_verified = verified
    provider = IdentityProviderRecord(id=uuid4(), provider_type="google", name="Google", tenant_id=fixture.other.id)
    link = AsyncMock()
    monkeypatch.setattr("app.services.sso_company_admission.find_pending_sso_user", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.sso_company_admission.org_member_dao.create", link)
    # When the SSO admission seam handles the provider identity.
    handled = await stage_sso_admission(fixture.applicant, provider, "external", target_id=fixture.other.id)
    # Then only verified email permits domain admission, never the conflicting SSO target.
    assert handled is True
    assert fixture.applicant.tenant_id is None
    assert fixture.identity.email_verified is verified
    assert len(fixture.admissions.rows) == int(verified)
    if verified:
        (request,) = fixture.admissions.rows.values()
        assert request.target_tenant_id == fixture.target.id
        assert request.method == AdmissionMethod.VERIFIED_DOMAIN
        assert request.source_tenant_id is None
    assert link.await_args is not None
    assert link.await_args.kwargs["obj_in"]["tenant_id"] is None
    assert link.await_args.kwargs["obj_in"]["user_id"] == fixture.applicant.id


async def test_registration_stages_verified_domain_request_without_moving_membership(
    domain_admission_fixture: DomainAdmissionFixture,
) -> None:
    # Given a newly registered, already-verified email in a claimed domain.
    fixture = domain_admission_fixture
    # When the real registration route is called over HTTP.
    response = await fixture.register()
    # Then an initial pending request exists without assigning any company membership.
    assert response.status_code == 201, response.text
    request = response.json()["admission_request"]
    assert request["method"] == "verified_domain"
    assert request["status"] == "pending"
    assert request["target_tenant"]["id"] == str(fixture.target.id)
    assert response.json()["user"]["tenant_id"] is None
    assert response.json()["needs_org_confirm"] is True
    assert response.json()["suggested_org"]["id"] == str(fixture.target.id)
    assert request["source_tenant_id"] is None
    assert request["transfer"] is None
    assert fixture.applicant.tenant_id is None
    assert len(fixture.admissions.rows) == 1
    (notice,) = fixture.notices
    assert (notice.user_id, notice.type, str(notice.ref_id)) == (
        fixture.genesis.id,
        "company_admission_pending",
        request["id"],
    )


@pytest.mark.parametrize("email_verified", [False, True])
async def test_registration_without_verified_domain_match_creates_no_request(
    domain_admission_fixture: DomainAdmissionFixture,
    email_verified: bool,
) -> None:
    # Given either an unverified matched email or a verified unmatched email.
    fixture = domain_admission_fixture
    fixture.identity.email_verified = email_verified
    if email_verified:
        fixture.identity.email = "member@unclaimed.example"
    # When registration completes.
    response = await fixture.register()
    # Then only an unmatched account joins KABERs; an unverified match stays unassigned.
    assert response.status_code == 201, response.text
    assert response.json()["admission_request"] is None
    assert response.json()["user"]["email_verified"] == email_verified
    assert fixture.applicant.tenant_id == (fixture.source.id if email_verified else None)
    assert not fixture.admissions.rows
    assert not fixture.notices


@pytest.mark.parametrize("domain_still_claimed", [False, True])
async def test_registration_replay_preserves_tenantless_pending_target(
    domain_admission_fixture: DomainAdmissionFixture,
    domain_still_claimed: bool,
) -> None:
    # Given an existing tenantless applicant with a pending company request.
    fixture = domain_admission_fixture
    fixture.registered = True
    fixture.applicant.tenant_id = None
    pending = await fixture.admissions.create(
        obj_in={
            "applicant_user_id": fixture.applicant.id,
            "source_tenant_id": None,
            "target_tenant_id": fixture.target.id,
            "method": AdmissionMethod.VERIFIED_DOMAIN.value,
        }
    )
    if not domain_still_claimed:
        fixture.identity.email = "member@unclaimed.example"
    # When registration is replayed with valid credentials.
    response = await fixture.register()
    # Then replay neither assigns KABERs nor overwrites the original admission.
    assert response.status_code == 201, response.text
    assert response.json()["user"]["tenant_id"] is None
    assert response.json()["needs_org_confirm"] is True
    assert list(fixture.admissions.rows.values()) == [pending]
    assert pending.target_tenant_id == fixture.target.id
    assert not fixture.notices


async def test_registration_invitation_takes_precedence_over_verified_domain(
    domain_admission_fixture: DomainAdmissionFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a verified domain match and an explicit invitation to a different company.
    fixture = domain_admission_fixture
    invitation = InvitationCodeRecord(id=uuid4(), code="explicit-invite", tenant_id=fixture.other.id)
    monkeypatch.setattr(invitation_code_dao, "get_active_by_code", AsyncMock(return_value=invitation))
    monkeypatch.setattr(invitation_code_dao, "lock_redeemable", AsyncMock(return_value=invitation))
    # When registering with the invitation.
    response = await fixture.register(invitation_code=invitation.code)
    # Then only the invitation request is staged, with no domain request or automatic approval.
    assert response.status_code == 201, response.text
    (request,) = fixture.admissions.rows.values()
    assert (request.method, request.status, request.target_tenant_id) == (
        AdmissionMethod.INVITATION,
        AdmissionStatus.PENDING,
        fixture.other.id,
    )
    assert fixture.applicant.tenant_id == fixture.source.id
    assert invitation.used_count == 0
    assert len(fixture.notices) == 1


@pytest.mark.parametrize("company_member", [False, True])
async def test_registration_does_not_reapply_domain_admission_to_existing_membership(
    domain_admission_fixture: DomainAdmissionFixture,
    company_member: bool,
) -> None:
    # Given an existing default-organization member registering again with valid credentials.
    fixture = domain_admission_fixture
    fixture.registered = True
    if company_member:
        fixture.applicant.tenant_id = fixture.other.id
    source_id = fixture.applicant.tenant_id
    # When registration is replayed.
    response = await fixture.register()
    # Then registration does not manufacture another admission entrypoint for existing accounts.
    assert response.status_code == 201, response.text
    assert response.json()["admission_request"] is None
    assert fixture.applicant.tenant_id == source_id
    assert not fixture.admissions.rows
    assert not fixture.notices


@pytest.mark.parametrize("inactive_identity", [False, True])
async def test_registration_skips_inactive_verified_accounts(
    domain_admission_fixture: DomainAdmissionFixture,
    inactive_identity: bool,
) -> None:
    # Given a verified identity or membership that is inactive.
    fixture = domain_admission_fixture
    if inactive_identity:
        fixture.identity.is_active = False
    else:
        fixture.applicant.is_active = False
    # When registration creates the default-organization membership.
    response = await fixture.register()
    # Then automatic admission does not reactivate or convert the account.
    assert response.status_code == 201, response.text
    assert response.json()["admission_request"] is None
    assert not fixture.admissions.rows
    assert not fixture.notices
