from dataclasses import replace
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException

from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod
from app.records.company_transfer import CompanyTransferRecord, TransferChoice, TransferStatus
from app.records.user import UserRecord
from app.schemas.company_transfer import PaymentDecision
from app.services.company_admissions import submit_request
from app.services.company_transfer_requests import TransferDestination, TransferSelection, submit_transfer
from app.services.company_transfer_review import decide_payment, require_payment_reviewer, withdraw_transfer
from company_transfer_fixture import TransferFixture, transfer_fixture

__all__ = ["transfer_fixture"]


async def paid(fixture: TransferFixture) -> CompanyTransferRecord:
    result = await submit_transfer(
        fixture.applicant,
        TransferDestination(fixture.target.id, AdmissionMethod.INVITATION, "JOIN"),
        TransferSelection(TransferChoice.PAID),
    )
    assert isinstance(result, CompanyTransferRecord)
    return result


async def test_paid_submission_snapshots_price_without_creating_admission(transfer_fixture: TransferFixture) -> None:
    # Given a default-organization member with agents choosing paid migration.
    fixture = transfer_fixture
    # When payment review is submitted.
    result = await paid(fixture)
    # Then only the immutable payment request exists, without target notices or membership changes.
    assert (result.fee_amount_minor, result.currency, result.status) == (3700, "USD", TransferStatus.PAYMENT_REVIEW)
    assert not fixture.admissions.rows
    assert not fixture.notices
    assert not fixture.emails
    assert fixture.applicant.tenant_id == fixture.source.id


async def test_confirm_creates_pending_admission_and_email_after_commit(transfer_fixture: TransferFixture) -> None:
    # Given an unconfirmed payment request.
    fixture = transfer_fixture
    request = await paid(fixture)
    # When the authorized payment reviewer confirms it in an owning transaction.
    async with connection_ctx():
        result = await decide_payment(request.id, fixture.reviewer.id, PaymentDecision(decision="approved"))
        assert not fixture.emails
    with anyio.fail_after(1):
        await fixture.email_ready.wait()
    # Then actual admission and both notices exist; email observes committed state, not an organization move.
    assert result.status == TransferStatus.ADMISSION_CREATED
    assert result.payment_confirmed_at is not None
    assert result.admission_request_id in fixture.admissions.rows
    assert fixture.notices == [
        (fixture.genesis.id, "company_admission_pending"),
        (fixture.applicant.id, "company_admission_created"),
    ]
    assert fixture.emails == [("applicant@example.test", 2)]
    assert fixture.applicant.tenant_id == fixture.source.id


async def test_repeated_confirmation_does_not_duplicate_admission_or_notices(transfer_fixture: TransferFixture) -> None:
    # Given a previously confirmed request.
    fixture = transfer_fixture
    request = await paid(fixture)
    confirmed = await decide_payment(request.id, fixture.reviewer.id, PaymentDecision(decision="approved"))
    with anyio.fail_after(1):
        await fixture.email_ready.wait()
    notices = list(fixture.notices)
    # When the same confirmation is replayed.
    replay = await decide_payment(request.id, fixture.reviewer.id, PaymentDecision(decision="approved"))
    # Then the original admission is returned without duplicate side effects.
    assert replay.admission_request_id == confirmed.admission_request_id
    assert len(fixture.admissions.rows) == 1
    assert fixture.notices == notices
    assert len(fixture.emails) == 1


async def test_rollback_drops_confirmation_email(transfer_fixture: TransferFixture) -> None:
    # Given an unconfirmed payment and a later failure in the same request transaction.
    fixture = transfer_fixture
    request = await paid(fixture)

    # When the owning transaction rolls back after the confirmation service returns.
    async def fail_confirmation() -> None:
        async with connection_ctx():
            await decide_payment(request.id, fixture.reviewer.id, PaymentDecision(decision="approved"))
            raise RuntimeError("response failed")

    with pytest.raises(RuntimeError, match="response failed"):
        await fail_confirmation()
    # Then the email callback is dropped (real row rollback is covered by opt-in PostgreSQL tests).
    assert not fixture.emails
    assert fixture.raw.rollbacks == 1


@pytest.mark.parametrize("operation", ["rejected", "withdrawn"])
async def test_payment_terminal_without_confirmation_creates_no_admission(
    transfer_fixture: TransferFixture, operation: str
) -> None:
    # Given an unconfirmed paid request.
    fixture = transfer_fixture
    request = await paid(fixture)
    # When it is rejected or withdrawn.
    if operation == "rejected":
        result = await decide_payment(request.id, fixture.reviewer.id, PaymentDecision(decision="rejected"))
    else:
        result = await withdraw_transfer(request.id, fixture.applicant.id)
    # Then no admission, notification, email, or agent disposition has occurred.
    assert result.status.value == operation
    assert not fixture.admissions.rows
    assert not fixture.notices
    assert not fixture.emails


async def test_free_requires_persisted_destructive_consent(transfer_fixture: TransferFixture) -> None:
    # Given an explicit free choice without consent.
    fixture = transfer_fixture
    # When the submission is attempted.
    with pytest.raises(HTTPException) as error:
        await submit_transfer(
            fixture.applicant,
            TransferDestination(fixture.target.id, AdmissionMethod.SSO),
            TransferSelection(TransferChoice.FREE),
        )
    # Then no request is created.
    assert error.value.status_code == 422
    assert not fixture.transfers.rows
    assert not fixture.admissions.rows


async def test_free_consent_creates_admission_without_payment_review(transfer_fixture: TransferFixture) -> None:
    # Given an explicitly consented free choice.
    fixture = transfer_fixture
    # When it is submitted.
    admission = await submit_transfer(
        fixture.applicant,
        TransferDestination(fixture.target.id, AdmissionMethod.SSO),
        TransferSelection(TransferChoice.FREE, True),
    )
    # Then consent is stored before pending admission, without moving or deleting any assets.
    transfer = await fixture.transfers.for_admission(admission.id)
    assert transfer is not None
    assert transfer.free_consent_at is not None
    assert transfer.status == TransferStatus.ADMISSION_CREATED
    assert transfer.payment_confirmed_at is None
    assert fixture.applicant.tenant_id == fixture.source.id


@pytest.mark.parametrize(("role", "genesis"), [("org_admin", False), ("member", True), ("member", False)])
async def test_arbitrary_default_org_member_cannot_review_payment(
    transfer_fixture: TransferFixture, role: str, genesis: bool
) -> None:
    # Given a default-org user without the genesis org-admin combination.
    fixture = transfer_fixture
    reviewer = UserRecord(id=uuid4(), tenant_id=fixture.source.id, role=role, is_genesis=genesis)
    fixture.users[reviewer.id] = reviewer
    # When payment-review authority is requested.
    with pytest.raises(HTTPException) as error:
        await require_payment_reviewer(reviewer.id)
    # Then no arbitrary org admin/member gets access.
    assert error.value.status_code == 403


async def test_target_genesis_cannot_confirm_payment(transfer_fixture: TransferFixture) -> None:
    # Given the target company's legitimate admission reviewer.
    fixture = transfer_fixture
    # When they try to acquire payment-review authority.
    with pytest.raises(HTTPException) as error:
        await require_payment_reviewer(fixture.genesis.id)
    # Then payment confirmation remains a separate authority.
    assert error.value.status_code == 403


async def test_pending_payment_cannot_be_bypassed_by_another_admission_entrypoint(
    transfer_fixture: TransferFixture,
) -> None:
    # Given a paid request awaiting manual confirmation.
    fixture = transfer_fixture
    await paid(fixture)
    # When an ordinary SSO/join seam tries to create the actual admission.
    with pytest.raises(HTTPException) as error:
        await submit_request(fixture.applicant, fixture.target.id, AdmissionMethod.SSO)
    # Then it is blocked without an admission.
    assert error.value.status_code == 409
    assert not fixture.admissions.rows


async def test_fake_active_reservation_releases_after_target_rejection(transfer_fixture: TransferFixture) -> None:
    # Given a linked admission that has been rejected by the target.
    fixture = transfer_fixture
    request = await paid(fixture)
    confirmed = await decide_payment(request.id, fixture.reviewer.id, PaymentDecision(decision="approved"))
    with anyio.fail_after(1):
        await fixture.email_ready.wait()
    assert confirmed.admission_request_id is not None
    from app.records.company_admission import AdmissionStatus

    admission = fixture.admissions.rows[confirmed.admission_request_id]
    fixture.admissions.rows[admission.id] = replace(admission, status=AdmissionStatus.REJECTED)
    # When the fake queries its active reservation using the production DAO's predicate.
    active = await fixture.transfers.active_for(fixture.applicant.id)
    # Then the completed admission no longer blocks a new transfer.
    assert active is None
