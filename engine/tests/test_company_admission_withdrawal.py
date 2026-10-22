from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter

from app.db.errors import CheckViolationError, UniqueViolationError
from app.schemas.company_admission import RequestDetail, RequestSummary
from app.services.company_admissions import withdraw_request
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]


async def test_repeated_withdrawal_keeps_original_terminal_detail(admission_scenario: AdmissionScenario):
    # Given an already withdrawn request.
    scenario = admission_scenario
    request_id = await scenario.submit()
    path = f"/api/company-admissions/{request_id}/withdraw"
    headers = scenario.headers(scenario.applicant)
    first = await scenario.client.post(path, headers=headers)
    assert first.status_code == 200, first.text

    # When withdrawal is repeated.
    repeated = await scenario.client.post(path, headers=headers)

    # Then every detail, including the terminal timestamp, is unchanged.
    assert repeated.status_code == 200, repeated.text
    assert RequestDetail.model_validate_json(repeated.content) == RequestDetail.model_validate_json(first.content)
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 1


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_withdrawal_conflicts_with_reviewer_terminal_state(admission_scenario: AdmissionScenario, decision: str):
    # Given a reviewer-resolved request.
    scenario = admission_scenario
    request_id = await scenario.submit()
    path = f"/api/company-admissions/{request_id}"
    resolved = await scenario.client.post(
        f"{path}/decision", json={"decision": decision}, headers=scenario.headers(scenario.genesis)
    )
    assert resolved.status_code == 200, resolved.text

    # When the applicant tries to withdraw.
    response = await scenario.client.post(f"{path}/withdraw", headers=scenario.headers(scenario.applicant))

    # Then the reviewer decision remains authoritative.
    assert response.status_code == 409
    persisted = await scenario.client.get(path, headers=scenario.headers(scenario.applicant))
    assert RequestDetail.model_validate_json(persisted.content) == RequestDetail.model_validate_json(resolved.content)


async def test_withdrawn_history_allows_a_later_pending_request(admission_scenario: AdmissionScenario):
    # Given a withdrawn request retained as history.
    scenario = admission_scenario
    first = await scenario.submit()
    withdrawn = await scenario.client.post(
        f"/api/company-admissions/{first}/withdraw", headers=scenario.headers(scenario.applicant)
    )
    assert withdrawn.status_code == 200, withdrawn.text

    # When the applicant submits a later request.
    later = await scenario.submit()

    # Then the new pending request and the withdrawn history coexist.
    assert later != first
    assert await scenario.db.fetchall("SELECT status FROM company_admission_requests ORDER BY created_at") == [
        {"status": "withdrawn"},
        {"status": "pending"},
    ]
    with pytest.raises(UniqueViolationError):
        await scenario.db.execute(
            "INSERT INTO company_admission_requests (id,applicant_user_id,target_tenant_id,method) "
            "VALUES (%(id)s,%(user)s,%(target)s,'invitation')",
            {"id": uuid4(), "user": scenario.applicant.id, "target": scenario.target_id},
        )


async def test_withdrawn_history_is_readable_to_applicant_and_reviewer(admission_scenario: AdmissionScenario):
    # Given a withdrawn request.
    scenario = admission_scenario
    request_id = await scenario.submit()
    result = await scenario.client.post(
        f"/api/company-admissions/{request_id}/withdraw", headers=scenario.headers(scenario.applicant)
    )
    assert result.status_code == 200, result.text

    # When both principals load their history and reviewer pending queue.
    mine = await scenario.client.get("/api/company-admissions/mine", headers=scenario.headers(scenario.applicant))
    review = await scenario.client.get(
        "/api/company-admissions/review?status=withdrawn", headers=scenario.headers(scenario.genesis)
    )
    pending = await scenario.client.get("/api/company-admissions/review", headers=scenario.headers(scenario.genesis))
    detail = await scenario.client.get(
        f"/api/company-admissions/{request_id}", headers=scenario.headers(scenario.genesis)
    )

    # Then withdrawn is a readable terminal state, absent from the pending queue.
    assert mine.status_code == review.status_code == pending.status_code == detail.status_code == 200
    assert TypeAdapter(list[RequestSummary]).validate_json(mine.content)[0].status == "withdrawn"
    assert TypeAdapter(list[RequestDetail]).validate_json(review.content)[0].status == "withdrawn"
    assert TypeAdapter(list[RequestDetail]).validate_json(pending.content) == []
    assert RequestDetail.model_validate_json(detail.content) == RequestDetail.model_validate_json(result.content)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE users SET is_active=false WHERE id=%(id)s",
        "UPDATE identities SET is_active=false WHERE id=(SELECT identity_id FROM users WHERE id=%(id)s)",
        "UPDATE identities SET must_change_password=true WHERE id=(SELECT identity_id FROM users WHERE id=%(id)s)",
    ],
)
async def test_withdrawal_rechecks_fresh_applicant(admission_scenario: AdmissionScenario, mutation: str):
    # Given a previously authenticated applicant whose authorization has changed.
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute(mutation, {"id": scenario.applicant.id})

    # When the service receives the stale principal's id directly.
    with pytest.raises(HTTPException) as error:
        await withdraw_request(UUID(request_id), scenario.applicant.id)

    # Then fresh membership/identity/password gates block the terminal transition.
    assert error.value.status_code == 403
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"


@pytest.mark.parametrize("with_reviewer", [False, True])
async def test_withdrawn_baseline_requires_timestamp_without_reviewer(
    admission_scenario: AdmissionScenario, with_reviewer: bool
):
    # Given the fresh-install baseline and a pending request.
    scenario = admission_scenario
    await scenario.submit()

    # When persisted withdrawal metadata violates its terminal contract.
    with pytest.raises(CheckViolationError):
        await scenario.db.execute(
            "UPDATE company_admission_requests SET status='withdrawn',"
            "decided_at=%(at)s,decided_by_user_id=%(reviewer)s",
            {
                "at": datetime(2026, 10, 21, tzinfo=UTC) if with_reviewer else None,
                "reviewer": scenario.genesis.id if with_reviewer else None,
            },
        )

    # Then PostgreSQL rejects the transition, retaining the pending request.
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"


async def test_missing_withdrawal_request_returns_not_found(admission_scenario: AdmissionScenario):
    # Given an applicant and an unknown request id.
    scenario = admission_scenario

    # When the applicant attempts withdrawal.
    response = await scenario.client.post(
        f"/api/company-admissions/{uuid4()}/withdraw", headers=scenario.headers(scenario.applicant)
    )

    # Then the route reports a missing request without creating history.
    assert response.status_code == 404
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
