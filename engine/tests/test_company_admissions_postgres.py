from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.schemas.company_admission import RequestDetail
from company_admission_fixtures import AdmissionScenario, admission_scenario, review_page, seed_review

__all__ = ["admission_scenario"]


async def test_invitation_request_preserves_membership_and_notifies_only_genesis(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    request_id = await scenario.submit()
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0
    rows = await scenario.db.fetchall("SELECT user_id,type,link FROM notifications")
    assert rows == [
        {
            "user_id": scenario.genesis.id,
            "type": "company_admission_pending",
            "link": f"/admission-approvals/{request_id}",
        }
    ]


async def test_duplicate_request_is_idempotent(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    first = await scenario.submit()
    second = await scenario.submit()
    assert second == first
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 1


@pytest.mark.parametrize("actor", ["additional", "outsider", "applicant"])
async def test_only_target_genesis_may_decide(admission_scenario: AdmissionScenario, actor: str):
    scenario = admission_scenario
    request_id = await scenario.submit()
    actors = {"additional": scenario.additional, "outsider": scenario.outsider, "applicant": scenario.applicant}
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved", "reason": None},
        headers=scenario.headers(actors[actor]),
    )
    assert response.status_code == 403
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


async def test_approval_moves_membership_consumes_invitation_and_notifies_applicant(
    admission_scenario: AdmissionScenario,
):
    scenario = admission_scenario
    request_id = await scenario.submit()
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved", "reason": "Welcome"},
        headers=scenario.headers(scenario.genesis),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    user = await scenario.current_applicant()
    assert (user.tenant_id, user.role, user.quota_max_agents) == (scenario.target_id, "member", 9)
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 1
    assert (
        await scenario.db.fetchval("SELECT link FROM notifications WHERE type='company_admission_resolved'")
        == f"/app/admissions/{request_id}"
    )


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_same_decision_repeats_without_extra_notification(admission_scenario: AdmissionScenario, decision: str):
    scenario = admission_scenario
    request_id = await scenario.submit()
    path = f"/api/company-admissions/{request_id}/decision"
    first = await scenario.client.post(
        path, json={"decision": decision, "reason": None}, headers=scenario.headers(scenario.genesis)
    )
    repeated = await scenario.client.post(
        path, json={"decision": decision, "reason": "ignored"}, headers=scenario.headers(scenario.genesis)
    )
    conflicting = await scenario.client.post(
        path,
        json={"decision": "rejected" if decision == "approved" else "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    assert first.status_code == repeated.status_code == 200
    assert repeated.json() == first.json()
    assert conflicting.status_code == 409
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications WHERE type='company_admission_resolved'") == 1


async def test_source_drift_blocks_approval_but_not_rejection(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute("UPDATE users SET tenant_id=NULL WHERE id=%(id)s", {"id": scenario.applicant.id})
    path = f"/api/company-admissions/{request_id}/decision"
    approval = await scenario.client.post(
        path, json={"decision": "approved"}, headers=scenario.headers(scenario.genesis)
    )
    rejection = await scenario.client.post(
        path, json={"decision": "rejected"}, headers=scenario.headers(scenario.genesis)
    )
    assert approval.status_code == 409
    assert rejection.status_code == 200
    assert rejection.json()["status"] == "rejected"


async def test_expired_invitation_keeps_request_pending(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute("UPDATE invitation_codes SET is_active=false")
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    assert response.status_code == 409
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
async def test_review_page_continuation_uses_an_extra_row(admission_scenario: AdmissionScenario, count: int):
    # Given a queue below, at, or beyond a two-item boundary.
    scenario = admission_scenario
    stamp = datetime(2026, 10, 21, 12, 0, 0, 123456, tzinfo=UTC)
    await seed_review(scenario, [(number, stamp) for number in range(1, count + 1)])

    # When the first page and its explicit continuation are requested.
    first = await review_page(scenario, {"limit": 2})
    pages = [first]
    if first.next_cursor is not None:
        pages.append(await review_page(scenario, {"limit": 2, "cursor": first.next_cursor}))

    # Then exact boundaries do not advertise a spurious empty page.
    assert [[item.id.int for item in page.items] for page in pages] == [
        list(range(count, 0, -1))[offset : offset + 2] for offset in range(0, max(count, 1), 2)
    ]
    assert (first.next_cursor is not None) == (count > 2)
    assert pages[-1].next_cursor is None


async def test_review_pages_preserve_timestamp_ties_and_microseconds(admission_scenario: AdmissionScenario):
    # Given timestamp ties with adjacent microseconds and UUIDs opposing timestamp order.
    scenario = admission_scenario
    stamp = datetime(2026, 10, 21, 12, 0, 0, 123456, tzinfo=UTC)
    await seed_review(scenario, [(9, stamp), (8, stamp), (7, stamp), (1, stamp + timedelta(microseconds=1))])

    # When each continuation is followed, including a different valid page size.
    first = await review_page(scenario, {"limit": 2})
    assert first.next_cursor is not None
    second = await review_page(scenario, {"limit": 1, "cursor": first.next_cursor})
    assert second.next_cursor is not None
    third = await review_page(scenario, {"limit": 1, "cursor": second.next_cursor})

    # Then the tuple boundary is exclusive and preserves full PostgreSQL precision.
    assert [item.id.int for page in (first, second, third) for item in page.items] == [1, 9, 8, 7]
    assert first.items[-1].created_at == stamp
    assert third.next_cursor is None


async def test_review_pages_ignore_inserts_above_the_position(admission_scenario: AdmissionScenario):
    # Given a continuation for the first two of four pending requests.
    scenario = admission_scenario
    stamp = datetime(2026, 10, 21, tzinfo=UTC)
    await seed_review(scenario, [(number, stamp) for number in range(1, 5)])
    first = await review_page(scenario, {"limit": 2})
    assert first.next_cursor is not None
    await seed_review(scenario, [(5, stamp + timedelta(seconds=1))])

    # When traversal continues while a newer request exists above the position.
    following = await review_page(scenario, {"limit": 2, "cursor": first.next_cursor})

    # Then untouched older rows are neither skipped nor duplicated.
    assert [item.id for item in following.items] == [UUID(int=2), UUID(int=1)]
    assert following.next_cursor is None


async def test_request_detail_access_is_applicant_or_target_genesis(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    request_id = await scenario.submit()
    own = await scenario.client.get(
        f"/api/company-admissions/{request_id}", headers=scenario.headers(scenario.applicant)
    )
    denied = await scenario.client.get(
        f"/api/company-admissions/{request_id}", headers=scenario.headers(scenario.additional)
    )
    review = await scenario.client.get("/api/company-admissions/review", headers=scenario.headers(scenario.genesis))
    mine = await scenario.client.get("/api/company-admissions/mine", headers=scenario.headers(scenario.applicant))
    assert own.status_code == 200
    assert denied.status_code == 403
    assert review.json()[0]["applicant"]["id"] == str(scenario.applicant.id)
    assert "username" not in own.json()["applicant"]
    assert "username" not in review.json()[0]["applicant"]
    assert mine.json()[0]["id"] == request_id
    assert "applicant" not in mine.json()[0]


async def test_withdrawal_preserves_membership_invitation_and_session(admission_scenario: AdmissionScenario):
    # Given a pending invitation request and the applicant's existing session.
    scenario = admission_scenario
    request_id = await scenario.submit()
    headers = scenario.headers(scenario.applicant)
    before = await scenario.current_applicant()
    notifications = await scenario.db.fetchall("SELECT * FROM notifications")

    # When the applicant withdraws without a request body.
    response = await scenario.client.post(f"/api/company-admissions/{request_id}/withdraw", headers=headers)

    # Then history is terminal without any approval or authentication side effects.
    assert response.status_code == 200, response.text
    result = RequestDetail.model_validate_json(response.content)
    assert str(result.id) == request_id
    assert result.status == "withdrawn"
    assert result.decided_at is not None
    assert result.decided_by_user_id is None
    assert result.decision_reason is None
    assert result.applicant.id == scenario.applicant.id
    assert result.source_tenant_id == scenario.source_id
    assert result.target_tenant.id == scenario.target_id
    assert await scenario.current_applicant() == before
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0
    assert await scenario.db.fetchall("SELECT * FROM notifications") == notifications
    assert response.headers.get("set-cookie") is None
    assert (await scenario.client.get("/api/auth/me", headers=headers)).status_code == 200


@pytest.mark.parametrize("actor", ["genesis", "additional", "outsider"])
async def test_withdrawal_denies_other_users_after_session_switch(admission_scenario: AdmissionScenario, actor: str):
    # Given a request opened using the applicant's session.
    scenario = admission_scenario
    request_id = await scenario.submit()
    path = f"/api/company-admissions/{request_id}"
    assert (await scenario.client.get(path, headers=scenario.headers(scenario.applicant))).status_code == 200

    # When the same client switches to another membership before withdrawal.
    actors = {"genesis": scenario.genesis, "additional": scenario.additional, "outsider": scenario.outsider}
    response = await scenario.client.post(f"{path}/withdraw", headers=scenario.headers(actors[actor]))

    # Then neither reviewer privileges nor the old page's owner authorize withdrawal.
    assert response.status_code == 403
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"
