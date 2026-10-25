import pytest

from app.core.domain.users import UserRole
from app.dao.notification_dao import notification_dao
from app.records.company_admission import AdmissionMethod, AdmissionStatus
from app.records.company_transfer import TransferChoice
from app.records.identity import IdentityRecord
from domain_admission_fixture import DomainAdmissionFixture, domain_admission_fixture

__all__ = ["domain_admission_fixture"]


async def test_verification_stages_domain_request_after_email_ownership_is_verified(
    domain_admission_fixture: DomainAdmissionFixture,
) -> None:
    # Given an unverified newly registered default-organization member in a claimed domain.
    fixture = domain_admission_fixture
    fixture.identity.email_verified = False
    fixture.identity.is_active = False
    fixture.applicant.is_active = False
    registration = await fixture.register()
    assert registration.status_code == 201, registration.text
    assert registration.json()["admission_request"] is None
    # When the real verification route consumes the ownership token.
    response = await fixture.verify()
    # Then verification succeeds with the unchanged token response and a pending reviewer request.
    assert response.status_code == 200, response.text
    assert response.json()["access_token"] == "access-token"
    assert response.json()["identity"]["email_verified"] is True
    assert response.json()["user"]["tenant_id"] == str(fixture.source.id)
    assert "admission_request" not in response.json()
    (request,) = fixture.admissions.rows.values()
    assert (request.method, request.status, request.target_tenant_id) == (
        AdmissionMethod.VERIFIED_DOMAIN,
        AdmissionStatus.PENDING,
        fixture.target.id,
    )
    (notice,) = fixture.notices
    assert (notice.user_id, notice.type, notice.ref_id) == (fixture.genesis.id, "company_admission_pending", request.id)
    assert fixture.applicant.tenant_id == fixture.source.id


@pytest.mark.parametrize("same_target", [False, True])
async def test_verification_preserves_pending_invitation_even_for_another_target(
    domain_admission_fixture: DomainAdmissionFixture,
    same_target: bool,
) -> None:
    # Given an unverified member whose explicit invitation admission is already pending.
    fixture = domain_admission_fixture
    fixture.registered = True
    fixture.identity.email_verified = False
    pending = await fixture.admissions.create(
        obj_in={
            "applicant_user_id": fixture.applicant.id,
            "source_tenant_id": fixture.source.id,
            "target_tenant_id": fixture.target.id if same_target else fixture.other.id,
            "method": AdmissionMethod.INVITATION.value,
            "invitation_code": "original-invite",
        }
    )
    # When email ownership is verified.
    response = await fixture.verify()
    # Then the original request is untouched and verification is not blocked by a competing domain target.
    assert response.status_code == 200, response.text
    assert response.json()["identity"]["email_verified"] is True
    assert list(fixture.admissions.rows.values()) == [pending]
    assert pending.method == AdmissionMethod.INVITATION
    assert pending.invitation_code == "original-invite"
    assert not fixture.notices
    assert fixture.applicant.tenant_id == fixture.source.id


async def test_verification_does_not_duplicate_existing_domain_request(
    domain_admission_fixture: DomainAdmissionFixture,
) -> None:
    # Given a pending domain admission from an earlier registration/verification.
    fixture = domain_admission_fixture
    fixture.registered = True
    pending = await fixture.admissions.create(
        obj_in={
            "applicant_user_id": fixture.applicant.id,
            "source_tenant_id": fixture.source.id,
            "target_tenant_id": fixture.target.id,
            "method": AdmissionMethod.VERIFIED_DOMAIN.value,
        }
    )
    # When another valid ownership token is consumed.
    response = await fixture.verify()
    # Then no duplicate request or reviewer notification is created.
    assert response.status_code == 200, response.text
    assert list(fixture.admissions.rows.values()) == [pending]
    assert not fixture.notices


@pytest.mark.parametrize(
    "ineligible",
    [
        "company_member",
        "org_admin",
        "platform_admin",
        "genesis",
        "password_change",
        "unmatched",
        "inactive_company",
        "system_company",
        "default_company",
        "missing_reviewer",
        "inactive_reviewer",
        "reviewer_password_change",
        "reviewer_identity_inactive",
        "owned_agents",
        "active_transfer",
    ],
)
async def test_verification_succeeds_without_admission_when_member_or_target_is_ineligible(
    domain_admission_fixture: DomainAdmissionFixture,
    ineligible: str,
) -> None:
    # Given an account whose email can be verified but whose automatic company admission is ineligible.
    fixture = domain_admission_fixture
    fixture.registered = True
    fixture.identity.email_verified = False
    match ineligible:
        case "company_member":
            fixture.applicant.tenant_id = fixture.other.id
        case "org_admin" | "platform_admin":
            fixture.applicant.role = UserRole(ineligible)
        case "genesis":
            fixture.applicant.is_genesis = True
        case "password_change":
            fixture.identity.must_change_password = True
        case "unmatched":
            fixture.identity.email = "member@unclaimed.example"
        case "inactive_company":
            fixture.target.is_active = False
        case "system_company":
            fixture.target.is_system = True
        case "default_company":
            fixture.target.is_default_end_user_org = True
        case "missing_reviewer":
            del fixture.users[fixture.genesis.id]
        case "inactive_reviewer":
            fixture.genesis.is_active = False
        case "reviewer_password_change":
            fixture.genesis.identity = IdentityRecord(id=fixture.genesis.id, must_change_password=True)
        case "reviewer_identity_inactive":
            fixture.genesis.identity = IdentityRecord(id=fixture.genesis.id, is_active=False)
        case "owned_agents":
            fixture.owns_agents = True
        case "active_transfer":
            await fixture.transfers.create(
                obj_in={
                    "applicant_user_id": fixture.applicant.id,
                    "source_tenant_id": fixture.source.id,
                    "target_tenant_id": fixture.other.id,
                    "choice": TransferChoice.PAID.value,
                    "method": AdmissionMethod.VERIFIED_DOMAIN.value,
                    "fee_amount_minor": 0,
                    "currency": "USD",
                    "instructions": "",
                }
            )
        case _:
            raise AssertionError(ineligible)
    source_id = fixture.applicant.tenant_id
    # When ownership verification consumes its single-use token.
    response = await fixture.verify()
    # Then successful verification is not lost to an optional admission eligibility error.
    assert response.status_code == 200, response.text
    assert response.json()["identity"]["email_verified"] is True
    assert response.json()["access_token"] == "access-token"
    assert fixture.applicant.tenant_id == source_id
    assert not fixture.tokens
    assert not fixture.admissions.rows
    assert not fixture.notices


async def test_verification_commits_ownership_before_unexpected_admission_failure(
    domain_admission_fixture: DomainAdmissionFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an unverified member and an unexpected reviewer notification failure.
    fixture = domain_admission_fixture
    fixture.registered = True
    fixture.identity.email_verified = False

    async def fail_notification(**_kwargs: object) -> None:
        raise RuntimeError("notification storage unavailable")

    monkeypatch.setattr(notification_dao, "create", fail_notification)
    # When the optional admission fails after consuming the email ownership token.
    with pytest.raises(RuntimeError, match="notification storage unavailable"):
        await fixture.verify()
    # Then the exception is not swallowed, but verified ownership was committed before admission rolled back.
    assert fixture.raw.commits == 1
    assert fixture.raw.rollbacks == 1
    assert fixture.identity.email_verified is True
    assert not fixture.tokens
