from collections.abc import AsyncIterator
from uuid import UUID

import anyio
import pytest

from app.api import background_tasks
from app.dao.company_transfer_dao import company_transfer_dao
from app.db.errors import CheckViolationError
from app.schemas.company_transfer import PaymentReviewResponse, TransferSummary
from app.schemas.transfer_policy import TransferPolicy
from app.services import company_transfer_requests as requests, company_transfer_review as review
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]


@pytest.fixture
async def transfer_scenario(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdmissionScenario]:
    async def policy() -> TransferPolicy:
        return TransferPolicy(fee_amount_minor=2900, currency="USD", instructions="Isolated QA policy")

    async def email(*, to: str, subject: str, body: str) -> None:
        del to, subject, body

    monkeypatch.setattr(requests, "get_transfer_policy", policy)
    monkeypatch.setattr(review, "send_system_email", email)
    background_tasks.start_background_tasks()
    try:
        yield admission_scenario
    finally:
        await background_tasks.drain_background_tasks(grace_seconds=0)
        background_tasks.start_background_tasks()


async def submit_paid(scenario: AdmissionScenario) -> UUID:
    response = await scenario.client.post(
        "/api/tenants/transfer",
        headers=scenario.headers(scenario.applicant),
        json={"password": "qa-password", "invitation_code": "JOIN", "transfer_choice": "paid"},
    )
    assert response.status_code == 202, response.text
    return PaymentReviewResponse.model_validate_json(response.content).transfer_request.id


async def test_paid_review_is_separate_and_concurrent_confirmation_is_idempotent(
    transfer_scenario: AdmissionScenario,
) -> None:
    # Given a paid request in a unique opt-in PostgreSQL schema, with no admission yet.
    scenario = transfer_scenario
    request_id = await submit_paid(scenario)
    assert await scenario.db.fetchone("SELECT id FROM company_admission_requests") is None
    results: list[TransferSummary] = []

    async def confirm() -> None:
        response = await scenario.client.post(
            f"/api/company-admissions/transfers/{request_id}/decision",
            headers=scenario.headers(scenario.outsider),
            json={"decision": "approved"},
        )
        assert response.status_code == 200, response.text
        results.append(TransferSummary.model_validate_json(response.content))

    # When two reviewers confirm concurrently.
    async with anyio.create_task_group() as group:
        group.start_soon(confirm)
        group.start_soon(confirm)
    # Then exactly one real admission and one notice per recipient are committed.
    assert len(results) == 2
    assert results[0].admission_request_id == results[1].admission_request_id
    assert await scenario.db.fetchone("SELECT count(*) AS n FROM company_admission_requests") == {"n": 1}
    assert await scenario.db.fetchone("SELECT count(*) AS n FROM notifications") == {"n": 2}
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


async def test_confirmation_rollback_leaves_payment_unconfirmed_and_no_admission(
    transfer_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an unconfirmed paid request and an applicant-notification failure.
    scenario = transfer_scenario
    request_id = await submit_paid(scenario)

    async def fail_notice(_user_id: UUID, **_kwargs: object) -> None:
        raise RuntimeError("notification failed")

    monkeypatch.setattr(review, "send_notification", fail_notice)
    # When confirmation fails after attempting actual admission creation.
    with pytest.raises(RuntimeError, match="notification failed"):
        await scenario.client.post(
            f"/api/company-admissions/transfers/{request_id}/decision",
            headers=scenario.headers(scenario.outsider),
            json={"decision": "approved"},
        )
    # Then all transactional rows, including the target notice, roll back together.
    row = await company_transfer_dao.get(request_id)
    assert row is not None
    assert row.payment_confirmed_at is None
    assert row.admission_request_id is None
    assert await scenario.db.fetchone("SELECT id FROM company_admission_requests") is None
    assert await scenario.db.fetchone("SELECT id FROM notifications") is None


async def test_payment_snapshot_is_database_immutable(transfer_scenario: AdmissionScenario) -> None:
    # Given a submitted quote snapshot.
    scenario = transfer_scenario
    request_id = await submit_paid(scenario)
    # When a write attempts to alter its fee after submission.
    with pytest.raises(CheckViolationError):
        await scenario.db.execute(
            "UPDATE company_transfer_requests SET fee_amount_minor=1 WHERE id=%(id)s", {"id": request_id}
        )
    # Then PostgreSQL rejects the mutation rather than silently changing the applicant's terms.
    row = await company_transfer_dao.get(request_id)
    assert row is not None
    assert row.fee_amount_minor == 2900
