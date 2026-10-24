from typing import Never

import pytest
from pydantic import JsonValue

from app.records.company_admission import AdmissionStatus
from app.services.company_admission_cursor import ReviewCursor
from applicant_admission_fixtures import PAGE_PATH, STAMP, applicant_cursor, applicant_page, seed_applicant
from company_admission_fixtures import AdmissionScenario, admission_scenario, seed_review

__all__ = ["admission_scenario"]


async def test_applicant_page_supports_all_four_filters_and_summary_only_history(admission_scenario: AdmissionScenario):
    # Given own history across statuses and companies, mixed with another applicant.
    scenario = admission_scenario
    for number, status in enumerate(AdmissionStatus, start=1):
        await seed_applicant(scenario, [(number, STAMP)], status)
    await seed_review(scenario, [(99, STAMP)])
    await scenario.db.execute(
        "UPDATE company_admission_requests SET target_tenant_id=%(tenant)s WHERE id=%(id)s",
        {"tenant": scenario.source_id, "id": (await seed_applicant(scenario, [(5, STAMP)]))[0]},
    )
    before = await scenario.current_applicant()

    # When the all-filter root and each explicit status are requested.
    all_requests = await applicant_page(scenario)
    filtered = {status: await applicant_page(scenario, {"status": status.value}) for status in AdmissionStatus}

    # Then only this user owns the summaries; omitted status includes every status and target.
    assert [item.id.int for item in all_requests.items] == [5, 4, 3, 2, 1]
    assert all_requests.next_cursor is None
    assert {status: [item.id.int for item in page.items] for status, page in filtered.items()} == {
        AdmissionStatus.PENDING: [1],
        AdmissionStatus.APPROVED: [2],
        AdmissionStatus.REJECTED: [5, 3],
        AdmissionStatus.WITHDRAWN: [4],
    }
    assert {item.target_tenant.id for item in all_requests.items} == {scenario.source_id, scenario.target_id}
    assert await scenario.current_applicant() == before
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 0


async def test_applicant_page_remains_user_scoped_without_a_tenant(admission_scenario: AdmissionScenario):
    # Given an applicant whose membership and historical source company are absent.
    scenario = admission_scenario
    await seed_applicant(scenario, [(1, STAMP), (2, STAMP)])
    await seed_review(scenario, [(99, STAMP)])
    await scenario.db.execute("UPDATE users SET tenant_id=NULL WHERE id=%(id)s", {"id": scenario.applicant.id})
    await scenario.db.execute(
        "UPDATE company_admission_requests SET source_tenant_id=NULL WHERE applicant_user_id=%(id)s",
        {"id": scenario.applicant.id},
    )

    # When traversing history with the same authenticated user and no tenant binding.
    first = await applicant_page(scenario, {"limit": 1})
    assert first.next_cursor is not None
    second = await applicant_page(scenario, {"limit": 1, "cursor": first.next_cursor})

    # Then both own requests remain accessible, without leaking another applicant.
    assert [item.id.int for page in (first, second) for item in page.items] == [2, 1]
    assert all(item.source_tenant_id is None for page in (first, second) for item in page.items)
    assert second.next_cursor is None
    assert (await scenario.current_applicant()).tenant_id is None


async def test_applicant_page_validates_query_bounds_and_unknown_status(admission_scenario: AdmissionScenario):
    # Given an authenticated applicant and invalid query parameter values.
    scenario = admission_scenario
    queries = [
        {"status": "all"},
        {"status": "unknown"},
        {"status": ""},
        {"limit": 0},
        {"limit": 101},
        {"limit": "invalid"},
        {"cursor": "x" * 2049},
    ]

    # When each invalid value is submitted through FastAPI validation.
    responses = [
        await scenario.client.get(PAGE_PATH, params=query, headers=scenario.headers(scenario.applicant))
        for query in queries
    ]

    # Then invalid filters, limits, and oversized cursors fail with 422, not unfiltered history.
    assert [response.status_code for response in responses] == [422] * len(queries)
    bounded = await scenario.client.get(
        PAGE_PATH, params={"cursor": "x" * 2048}, headers=scenario.headers(scenario.applicant)
    )
    assert bounded.status_code == 400
    assert bounded.json() == {"detail": "Invalid cursor"}


async def test_applicant_page_rejects_malformed_or_mismatched_positions_uniformly(
    admission_scenario: AdmissionScenario,
):
    # Given owner/filter/kind/version mismatches, including a valid reviewer position.
    scenario = admission_scenario
    changes: list[dict[str, JsonValue]] = [
        {"user_id": str(scenario.additional.id)},
        {"status": "pending"},
        {"version": 2},
        {"kind": "review"},
    ]
    cursors = [
        "",
        "%%%",
        "é",
        "_w",
        *(applicant_cursor({"user_id": str(scenario.applicant.id), **change}) for change in changes),
    ]
    cursors.append(
        ReviewCursor(
            version=1,
            tenant_id=scenario.target_id,
            status=AdmissionStatus.PENDING,
            created_at=STAMP,
            id=scenario.applicant.id,
        ).encode()
    )

    # When an authenticated applicant submits any invalid or mismatched position.
    responses = [
        await scenario.client.get(PAGE_PATH, params={"cursor": cursor}, headers=scenario.headers(scenario.applicant))
        for cursor in cursors
    ]

    # Then the same exact error covers malformed and valid-but-foreign cursor payloads.
    assert [(response.status_code, response.json()) for response in responses] == [
        (400, {"detail": "Invalid cursor"})
    ] * len(cursors)


async def test_applicant_cursor_is_bound_to_user_and_canonical_filter(admission_scenario: AdmissionScenario):
    # Given real continuations for all requests and explicitly rejected requests.
    scenario = admission_scenario
    await seed_applicant(scenario, [(1, STAMP), (2, STAMP)])
    all_requests = await applicant_page(scenario, {"limit": 1})
    rejected = await applicant_page(scenario, {"limit": 1, "status": "rejected"})
    assert all_requests.next_cursor is not None
    assert rejected.next_cursor is not None

    # When owner or canonical filter changes without resetting the cursor.
    switched = await scenario.client.get(
        PAGE_PATH, params={"cursor": all_requests.next_cursor}, headers=scenario.headers(scenario.additional)
    )
    filtered = await scenario.client.get(
        PAGE_PATH,
        params={"cursor": all_requests.next_cursor, "status": "rejected"},
        headers=scenario.headers(scenario.applicant),
    )
    unfiltered = await scenario.client.get(
        PAGE_PATH, params={"cursor": rejected.next_cursor}, headers=scenario.headers(scenario.applicant)
    )

    # Then both directions of the filter binding and the retired owner fail uniformly.
    assert [(response.status_code, response.json()) for response in (switched, filtered, unfiltered)] == [
        (400, {"detail": "Invalid cursor"})
    ] * 3


@pytest.mark.parametrize("revocation", ["membership", "identity", "password", "generation", "missing", "invalid"])
async def test_applicant_authentication_precedes_cursor_decode(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, revocation: str
):
    # Given an existing cursor whose session subsequently loses authentication or force-change clearance.
    scenario = admission_scenario
    await seed_applicant(scenario, [(1, STAMP), (2, STAMP)])
    first = await applicant_page(scenario, {"limit": 1})
    assert first.next_cursor is not None
    updates = {
        "membership": ("UPDATE users SET is_active=false WHERE id=%(id)s", scenario.applicant.id),
        "identity": ("UPDATE identities SET is_active=false WHERE id=%(id)s", scenario.applicant.identity_id),
        "password": ("UPDATE identities SET must_change_password=true WHERE id=%(id)s", scenario.applicant.identity_id),
        "generation": (
            "UPDATE identities SET auth_generation=auth_generation+1 WHERE id=%(id)s",
            scenario.applicant.identity_id,
        ),
    }
    if revocation in updates:
        sql, identifier = updates[revocation]
        await scenario.db.execute(sql, {"id": identifier})
    headers = scenario.headers(scenario.applicant)
    if revocation == "missing":
        headers = {}
    if revocation == "invalid":
        headers = {"Authorization": "Bearer invalid"}

    def forbidden_decode(_cursor: str) -> Never:
        raise AssertionError("Cursor decoding ran before authentication")

    monkeypatch.setattr("app.api.company_admissions.ApplicantCursor.decode", forbidden_decode)

    # When both valid and malformed cursor inputs arrive using that session.
    responses = [
        await scenario.client.get(PAGE_PATH, params={"cursor": cursor}, headers=headers)
        for cursor in (first.next_cursor, "%%%")
    ]

    # Then current-user authentication rejects both calls without invoking the decoder.
    assert [response.status_code for response in responses] == [403 if revocation == "password" else 401] * 2
