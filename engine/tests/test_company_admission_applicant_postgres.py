from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from app.dao.company_admission_dao import company_admission_dao
from app.records.company_admission import AdmissionStatus
from app.schemas.company_admission import RequestDetail
from applicant_admission_fixtures import STAMP, ApplicantSummaryResult, applicant_page, seed_applicant
from company_admission_fixtures import AdmissionScenario, admission_scenario, review_page

__all__ = ["admission_scenario"]


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
async def test_applicant_page_lookahead_handles_empty_partial_and_exact_pages(
    admission_scenario: AdmissionScenario, count: int
):
    # Given own terminal history below, at, or beyond a two-item boundary.
    scenario = admission_scenario
    await seed_applicant(scenario, [(number, STAMP) for number in range(1, count + 1)])

    # When the first page and any advertised continuation are requested.
    first = await applicant_page(scenario, {"limit": 2})
    pages = [first]
    if first.next_cursor is not None:
        pages.append(await applicant_page(scenario, {"limit": 2, "cursor": first.next_cursor}))

    # Then exact final pages do not advertise a spurious empty page.
    assert [[item.id.int for item in page.items] for page in pages] == [
        list(range(count, 0, -1))[offset : offset + 2] for offset in range(0, max(count, 1), 2)
    ]
    assert (first.next_cursor is not None) == (count > 2)
    assert pages[-1].next_cursor is None


async def test_applicant_page_preserves_timestamp_ties_microseconds_and_changed_limit(
    admission_scenario: AdmissionScenario,
):
    # Given UUID ties opposing timestamp order, with a one-microsecond timestamp boundary.
    scenario = admission_scenario
    await seed_applicant(scenario, [(9, STAMP), (8, STAMP), (7, STAMP), (1, STAMP + timedelta(microseconds=1))])

    # When one-item then two-item pages traverse both timestamp and UUID seek boundaries.
    first = await applicant_page(scenario, {"limit": 1})
    assert first.next_cursor is not None
    second = await applicant_page(scenario, {"limit": 2, "cursor": first.next_cursor})
    assert second.next_cursor is not None
    third = await applicant_page(scenario, {"limit": 1, "cursor": second.next_cursor})

    # Then full precision and exclusive descending tuple ordering retain every row once.
    assert [item.id.int for page in (first, second, third) for item in page.items] == [1, 9, 8, 7]
    assert first.items[0].created_at == STAMP + timedelta(microseconds=1)
    assert second.items[0].created_at == STAMP
    assert third.next_cursor is None


async def test_applicant_seek_sql_filters_status_and_owner_on_both_sides_of_boundary(
    admission_scenario: AdmissionScenario,
):
    # Given interleaved statuses and a tenantless membership sharing the same identity.
    scenario = admission_scenario
    ids = await seed_applicant(scenario, [(number, STAMP) for number in range(1, 8)])
    await scenario.db.execute(
        "UPDATE company_admission_requests SET status='approved' WHERE id=ANY(%(ids)s)", {"ids": [ids[1], ids[4]]}
    )
    await scenario.db.execute(
        "UPDATE users SET identity_id=%(identity)s,tenant_id=NULL WHERE id=%(id)s",
        {"identity": scenario.applicant.identity_id, "id": scenario.outsider.id},
    )
    await scenario.db.execute(
        "UPDATE company_admission_requests SET applicant_user_id=%(owner)s WHERE id=ANY(%(ids)s)",
        {"owner": scenario.outsider.id, "ids": [ids[0], ids[5]]},
    )

    # When the actual applicant DAO applies lookahead and an exclusive position to a selected status.
    first = await company_admission_dao.list_mine_page(scenario.applicant.id, AdmissionStatus.REJECTED, limit=1)
    second = await company_admission_dao.list_mine_page(
        scenario.applicant.id, AdmissionStatus.REJECTED, limit=2, before=(first[0].created_at, first[0].id)
    )

    # Then the limit-plus-one and subsequent seek exclude both foreign users and wrong statuses.
    assert [row.id.int for row in first] == [7, 4]
    assert [row.id.int for row in second] == [4, 3]
    assert all(
        row.applicant_user_id == scenario.applicant.id and row.status is AdmissionStatus.REJECTED
        for row in first + second
    )


async def test_applicant_pages_ignore_newer_inserts_above_the_position(admission_scenario: AdmissionScenario):
    # Given a continuation and a request inserted above its immutable tuple boundary.
    scenario = admission_scenario
    await seed_applicant(scenario, [(number, STAMP) for number in range(1, 5)])
    first = await applicant_page(scenario, {"limit": 2})
    assert first.next_cursor is not None
    await seed_applicant(scenario, [(5, STAMP + timedelta(seconds=1))])

    # When following the previously returned cursor.
    following = await applicant_page(scenario, {"limit": 2, "cursor": first.next_cursor})

    # Then no older request is skipped or duplicated, without promising a snapshot.
    assert [item.id for item in following.items] == [UUID(int=2), UUID(int=1)]
    assert following.next_cursor is None


@pytest.mark.parametrize("transition", ["approved", "rejected", "withdrawn"])
async def test_applicant_history_and_pending_filter_refresh_after_status_transition(
    admission_scenario: AdmissionScenario, transition: str
):
    # Given a pending request above older history and a saved all-history continuation.
    scenario = admission_scenario
    await seed_applicant(scenario, [(1, STAMP), (2, STAMP)])
    request_id = await scenario.submit()
    first = await applicant_page(scenario, {"limit": 1})
    assert str(first.items[0].id) == request_id
    assert first.next_cursor is not None
    assert [str(item.id) for item in (await applicant_page(scenario, {"status": "pending"})).items] == [request_id]
    if transition == "withdrawn":
        changed = await scenario.client.post(
            f"/api/company-admissions/{request_id}/withdraw", headers=scenario.headers(scenario.applicant)
        )
    else:
        changed = await scenario.client.post(
            f"/api/company-admissions/{request_id}/decision",
            json={"decision": transition},
            headers=scenario.headers(scenario.genesis),
        )
    assert changed.status_code == 200, changed.text
    membership_after_transition = await scenario.current_applicant()

    # When continuing the old history and refreshing the selected filters after the transition.
    following = await applicant_page(scenario, {"limit": 2, "cursor": first.next_cursor})
    pending = await applicant_page(scenario, {"status": "pending"})
    resolved = await applicant_page(scenario, {"status": transition})

    # Then older history stays reachable while fresh filters reflect current state without read-side membership changes.
    assert [item.id.int for item in following.items] == [2, 1]
    assert following.next_cursor is None
    assert pending.items == []
    assert pending.next_cursor is None
    assert str(resolved.items[0].id) == request_id
    assert resolved.items[0].status.value == transition
    assert await scenario.current_applicant() == membership_after_transition


async def test_legacy_mine_review_and_withdrawal_contracts_remain_unchanged(admission_scenario: AdmissionScenario):
    # Given terminal history and a pending request, read through both API generations.
    scenario = admission_scenario
    await seed_applicant(scenario, [(1, STAMP), (2, STAMP)])
    request_id = await scenario.submit()
    await applicant_page(scenario, {"status": "pending"})

    # When legacy offset arrays, reviewer detail pages, and idempotent withdrawal are still used.
    mine = await scenario.client.get(
        "/api/company-admissions/mine", params={"limit": 1, "offset": 1}, headers=scenario.headers(scenario.applicant)
    )
    review = await review_page(scenario)
    path = f"/api/company-admissions/{request_id}/withdraw"
    withdrawal = await scenario.client.post(path, headers=scenario.headers(scenario.applicant))
    repeated = await scenario.client.post(path, headers=scenario.headers(scenario.applicant))

    # Then arrays, reviewer-only details, and repeated withdrawal retain their existing contracts.
    assert mine.status_code == 200
    assert [item.id for item in TypeAdapter(list[ApplicantSummaryResult]).validate_json(mine.content)] == [UUID(int=2)]
    assert str(review.items[0].id) == request_id
    assert review.items[0].applicant.id == scenario.applicant.id
    assert review.next_cursor is None
    assert withdrawal.status_code == repeated.status_code == 200
    assert withdrawal.json() == repeated.json()
    assert RequestDetail.model_validate_json(withdrawal.content).status is AdmissionStatus.WITHDRAWN
