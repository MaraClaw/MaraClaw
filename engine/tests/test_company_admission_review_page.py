from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from json import dumps
from uuid import uuid4

import pytest
from pydantic import JsonValue

from app.records.company_admission import AdmissionStatus
from company_admission_fixtures import AdmissionScenario, admission_scenario, review_page, seed_review

__all__ = ["admission_scenario"]

STAMP = datetime(2026, 10, 21, 12, 0, 0, 123456, tzinfo=UTC)


def cursor_for(scenario: AdmissionScenario, changes: dict[str, JsonValue] | None = None) -> str:
    payload: dict[str, JsonValue] = {
        "version": 1,
        "tenant_id": str(scenario.target_id),
        "status": "pending",
        "created_at": STAMP.isoformat(),
        "id": str(uuid4()),
    }
    payload.update(changes or {})
    return urlsafe_b64encode(dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize("transition", ["approved", "rejected", "withdrawn"])
async def test_continuation_survives_an_earlier_request_leaving_pending(
    admission_scenario: AdmissionScenario, transition: str
):
    # Given a first page whose leading request is later decided or withdrawn.
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute(
        "UPDATE company_admission_requests SET created_at=%(stamp)s WHERE id=%(id)s",
        {"stamp": STAMP + timedelta(seconds=1), "id": request_id},
    )
    await seed_review(scenario, [(number, STAMP) for number in range(1, 4)])
    first = await review_page(scenario, {"limit": 2})
    assert str(first.items[0].id) == request_id
    assert first.next_cursor is not None
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

    # When the existing continuation is followed after the queue shrinks.
    following = await review_page(scenario, {"limit": 2, "cursor": first.next_cursor})

    # Then both untouched requests remain reachable, unlike offset pagination.
    assert [item.id.int for item in following.items] == [2, 1]
    assert following.next_cursor is None


@pytest.mark.parametrize("status", list(AdmissionStatus))
async def test_review_page_sql_scopes_tenant_and_selected_status(
    admission_scenario: AdmissionScenario, status: AdmissionStatus
):
    # Given interleaved tenants/statuses on both sides of a keyset boundary.
    scenario = admission_scenario
    ids = await seed_review(scenario, [(number, STAMP) for number in range(1, 10)])
    await scenario.db.execute(
        "UPDATE company_admission_requests SET target_tenant_id=%(tenant)s WHERE id=ANY(%(ids)s)",
        {"tenant": scenario.source_id, "ids": [ids[1], ids[4], ids[7]]},
    )
    await scenario.db.execute(
        "UPDATE company_admission_requests SET status=%(status)s,decided_at=%(decided)s WHERE id=ANY(%(ids)s)",
        {"status": status.value, "decided": None if status is AdmissionStatus.PENDING else STAMP, "ids": ids},
    )
    other_status = AdmissionStatus.REJECTED if status is AdmissionStatus.PENDING else AdmissionStatus.PENDING
    await scenario.db.execute(
        "UPDATE company_admission_requests SET status=%(status)s,decided_at=%(decided)s WHERE id=ANY(%(ids)s)",
        {
            "status": other_status.value,
            "decided": None if other_status is AdmissionStatus.PENDING else STAMP,
            "ids": [ids[6], ids[8]],
        },
    )

    # When both first-page and cursor SQL execute for the selected status.
    first = await review_page(scenario, {"status": status.value, "limit": 2})
    assert first.next_cursor is not None
    second = await review_page(scenario, {"status": status.value, "limit": 2, "cursor": first.next_cursor})

    # Then no foreign tenant or other status leaks through either query.
    assert [item.id.int for page in (first, second) for item in page.items] == [6, 4, 3, 1]
    assert all(
        item.target_tenant.id == scenario.target_id and item.status is status
        for page in (first, second)
        for item in page.items
    )
    assert second.next_cursor is None


@pytest.mark.parametrize(
    "cursor",
    ["", "%%%", "é", "a", "_w", urlsafe_b64encode(b"not json").decode(), urlsafe_b64encode(b"[]").decode()],
)
async def test_review_page_rejects_malformed_cursor(admission_scenario: AdmissionScenario, cursor: str):
    # Given a freshly authorized reviewer and malformed base64, UTF-8, or JSON.
    scenario = admission_scenario

    # When the malformed position crosses the real HTTP boundary.
    response = await scenario.client.get(
        "/api/company-admissions/review/page", params={"cursor": cursor}, headers=scenario.headers(scenario.genesis)
    )

    # Then it fails as a client error rather than becoming an unbounded list.
    assert response.status_code == 400


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"version": 1.0},
        {"version": "1"},
        {"tenant_id": "invalid"},
        {"tenant_id": str(uuid4())},
        {"status": "approved"},
        {"status": "invalid"},
        {"created_at": "2026-10-21T12:00:00"},
        {"created_at": 1792584000},
        {"created_at": None},
        {"id": "invalid"},
        {"id": 1},
        {"id": None},
        {"unexpected": "field"},
    ],
)
async def test_review_page_rejects_invalid_or_mismatched_cursor_fields(
    admission_scenario: AdmissionScenario, changes: dict[str, JsonValue]
):
    # Given an unsigned position with invalid types, version, or tenant/status binding.
    scenario = admission_scenario

    # When an otherwise authorized reviewer submits it.
    response = await scenario.client.get(
        "/api/company-admissions/review/page",
        params={"cursor": cursor_for(scenario, changes)},
        headers=scenario.headers(scenario.genesis),
    )

    # Then parsing and binding reject the entire cursor with HTTP 400.
    assert response.status_code == 400


@pytest.mark.parametrize("revocation", ["genesis", "membership", "identity", "password", "tenant"])
@pytest.mark.parametrize("cursor_kind", ["valid", "malformed"])
async def test_review_page_fresh_authorization_precedes_cursor_validation(
    admission_scenario: AdmissionScenario, revocation: str, cursor_kind: str
):
    # Given a valid first page and an old authenticated session that loses authority.
    scenario = admission_scenario
    await seed_review(scenario, [(1, STAMP), (2, STAMP)])
    first = await review_page(scenario, {"limit": 1})
    assert first.next_cursor is not None
    headers = scenario.headers(scenario.genesis)
    updates = {
        "genesis": ("UPDATE users SET is_genesis=false WHERE id=%(id)s", scenario.genesis.id),
        "membership": ("UPDATE users SET is_active=false WHERE id=%(id)s", scenario.genesis.id),
        "identity": ("UPDATE identities SET is_active=false WHERE id=%(id)s", scenario.genesis.identity_id),
        "password": ("UPDATE identities SET must_change_password=true WHERE id=%(id)s", scenario.genesis.identity_id),
        "tenant": ("UPDATE tenants SET is_active=false WHERE id=%(id)s", scenario.target_id),
    }
    sql, identifier = updates[revocation]
    await scenario.db.execute(sql, {"id": identifier})

    # When the next call reuses that session and even an invalid cursor.
    response = await scenario.client.get(
        "/api/company-admissions/review/page",
        params={"cursor": first.next_cursor if cursor_kind == "valid" else "%%%"},
        headers=headers,
    )

    # Then neither possession nor parsing of a cursor authorizes the reviewer.
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("actor", ["additional", "outsider", "applicant"])
async def test_review_page_denies_non_genesis_sessions(admission_scenario: AdmissionScenario, actor: str):
    # Given an additional admin, platform admin, or ordinary applicant session.
    scenario = admission_scenario
    users = {"additional": scenario.additional, "outsider": scenario.outsider, "applicant": scenario.applicant}

    # When a forged but correctly bound unsigned position is submitted.
    response = await scenario.client.get(
        "/api/company-admissions/review/page",
        params={"cursor": cursor_for(scenario)},
        headers=scenario.headers(users[actor]),
    )

    # Then the server independently requires current genesis-review authority.
    assert response.status_code == 403
