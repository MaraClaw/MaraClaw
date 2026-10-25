from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dao.invitation_code_dao import invitation_code_dao
from app.records.company_admission import AdmissionMethod, AdmissionStatus
from app.records.invitation import InvitationCodeRecord
from domain_admission_fixture import DomainAdmissionFixture, domain_admission_fixture

__all__ = ["domain_admission_fixture"]


async def test_registration_stages_verified_domain_request_without_moving_membership(
    domain_admission_fixture: DomainAdmissionFixture,
) -> None:
    # Given a newly registered, already-verified email in a claimed domain.
    fixture = domain_admission_fixture
    # When the real registration route is called over HTTP.
    response = await fixture.register()
    # Then a pending request and reviewer notification exist, while membership remains in OpenClaw.
    assert response.status_code == 201, response.text
    request = response.json()["admission_request"]
    assert request["method"] == "verified_domain"
    assert request["status"] == "pending"
    assert request["target_tenant"]["id"] == str(fixture.target.id)
    assert response.json()["user"]["tenant_id"] == str(fixture.source.id)
    assert fixture.applicant.tenant_id == fixture.source.id
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
    # Then the account remains in OpenClaw without admission side effects.
    assert response.status_code == 201, response.text
    assert response.json()["admission_request"] is None
    assert response.json()["user"]["email_verified"] == email_verified
    assert fixture.applicant.tenant_id == fixture.source.id
    assert not fixture.admissions.rows
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
