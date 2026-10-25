from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dao.invitation_code_dao import invitation_code_dao
from app.records.company_admission import AdmissionMethod, AdmissionStatus
from app.records.invitation import InvitationCodeRecord
from domain_admission_fixture import DomainAdmissionFixture, domain_admission_fixture

__all__ = ["domain_admission_fixture"]


@pytest.mark.parametrize("status", [AdmissionStatus.REJECTED, AdmissionStatus.WITHDRAWN])
@pytest.mark.parametrize("same_target", [False, True])
async def test_verification_preserves_resolved_signup_invitation(
    domain_admission_fixture: DomainAdmissionFixture,
    monkeypatch: pytest.MonkeyPatch,
    status: AdmissionStatus,
    same_target: bool,
) -> None:
    # Given an unverified signup with an explicit invitation, resolved before email verification.
    fixture = domain_admission_fixture
    fixture.identity.email_verified = False
    target = fixture.target if same_target else fixture.other
    invitation = InvitationCodeRecord(id=uuid4(), code="signup-invitation", tenant_id=target.id)
    monkeypatch.setattr(invitation_code_dao, "get_active_by_code", AsyncMock(return_value=invitation))
    monkeypatch.setattr(invitation_code_dao, "lock_redeemable", AsyncMock(return_value=invitation))
    registration = await fixture.register(invitation_code=invitation.code)
    assert registration.status_code == 201, registration.text
    (original,) = fixture.admissions.rows.values()
    original.status = status
    notices = list(fixture.notices)
    # When the signup verification link is redeemed over HTTP.
    response = await fixture.verify()
    # Then ownership verification succeeds without overriding invitation history or notifying another reviewer.
    assert response.status_code == 200, response.text
    assert response.json()["access_token"] == "access-token"
    assert response.json()["identity"]["email_verified"] is True
    assert list(fixture.admissions.rows.values()) == [original]
    assert (original.method, original.status, original.target_tenant_id) == (
        AdmissionMethod.INVITATION,
        status,
        target.id,
    )
    assert fixture.notices == notices
    assert fixture.applicant.tenant_id == fixture.source.id
    assert invitation.used_count == 0
