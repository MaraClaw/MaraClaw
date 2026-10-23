from typing import Literal, assert_never

import pytest
from httpx import Response
from pydantic import TypeAdapter

from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.notification_responses import NotificationResponse, NotificationUnreadCountResponse
from app.schemas.schemas import RegisterInitResponse
from app.services import company_admission_decisions
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]

NOTIFICATIONS = TypeAdapter(list[NotificationResponse])
UNREAD_COUNT = TypeAdapter(NotificationUnreadCountResponse)


async def notifications(scenario: AdmissionScenario, user: UserRecord) -> list[NotificationResponse]:
    response = await scenario.client.get("/api/notifications", headers=scenario.headers(user))
    assert response.status_code == 200, response.text
    return NOTIFICATIONS.validate_json(response.content)


async def unread_count(scenario: AdmissionScenario, user: UserRecord) -> int:
    response = await scenario.client.get("/api/notifications/unread-count", headers=scenario.headers(user))
    assert response.status_code == 200, response.text
    return UNREAD_COUNT.validate_json(response.content)["unread_count"]


async def approve(scenario: AdmissionScenario, request_id: str) -> Response:
    return await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )


async def test_approval_welcomes_only_the_applicant(admission_scenario: AdmissionScenario) -> None:
    # Given a pending company admission from the default organization.
    scenario = admission_scenario
    request_id = await scenario.submit()

    # When the target organization's genesis admin approves it.
    response = await approve(scenario, request_id)

    # Then the welcome is private to that membership, separate from the resolution.
    assert response.status_code == 200, response.text
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id
    assert await scenario.db.fetchall(
        "SELECT user_id,agent_id,ref_id FROM notifications WHERE type='organization_welcome'"
    ) == [{"user_id": scenario.applicant.id, "agent_id": None, "ref_id": scenario.target_id}]
    applicant_notifications = await notifications(scenario, scenario.applicant)
    assert {(item["type"], item["ref_id"]) for item in applicant_notifications} == {
        ("organization_welcome", str(scenario.target_id)),
        ("company_admission_resolved", request_id),
    }
    assert await unread_count(scenario, scenario.applicant) == 2
    for user in (scenario.genesis, scenario.additional, scenario.outsider):
        assert all(item["type"] != "organization_welcome" for item in await notifications(scenario, user))


@pytest.mark.parametrize("state", ["pending", "rejected", "withdrawn"])
async def test_unapproved_admission_has_no_target_welcome(
    admission_scenario: AdmissionScenario, state: Literal["pending", "rejected", "withdrawn"]
) -> None:
    # Given a pending request with no target membership yet.
    scenario = admission_scenario
    request_id = await scenario.submit()

    # When it stays pending, is rejected, or is withdrawn.
    match state:
        case "pending":
            pass
        case "rejected":
            response = await scenario.client.post(
                f"/api/company-admissions/{request_id}/decision",
                json={"decision": "rejected"},
                headers=scenario.headers(scenario.genesis),
            )
            assert response.status_code == 200, response.text
        case "withdrawn":
            response = await scenario.client.post(
                f"/api/company-admissions/{request_id}/withdraw", headers=scenario.headers(scenario.applicant)
            )
            assert response.status_code == 200, response.text
        case unreachable:
            assert_never(unreachable)

    # Then no welcome to the target exists, including through the applicant's API.
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == state
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications WHERE type='organization_welcome'") == 0
    assert all(item["type"] != "organization_welcome" for item in await notifications(scenario, scenario.applicant))


async def test_approval_replay_keeps_one_welcome(admission_scenario: AdmissionScenario) -> None:
    # Given an already approved request.
    scenario = admission_scenario
    request_id = await scenario.submit()
    first = await approve(scenario, request_id)
    assert first.status_code == 200, first.text

    # When the same approval is replayed.
    replay = await approve(scenario, request_id)

    # Then both independent notification types remain single-delivery.
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    rows = await notifications(scenario, scenario.applicant)
    assert sorted(item["type"] for item in rows) == ["company_admission_resolved", "organization_welcome"]
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications WHERE type='organization_welcome'") == 1


async def test_other_users_cannot_read_the_applicants_welcome(admission_scenario: AdmissionScenario) -> None:
    # Given an unread welcome and a separate admission-resolution notification.
    scenario = admission_scenario
    response = await approve(scenario, await scenario.submit())
    assert response.status_code == 200, response.text
    welcome = next(
        item for item in await notifications(scenario, scenario.applicant) if item["type"] == "organization_welcome"
    )

    # When same-tenant admins and a cross-tenant platform admin mark it/read-all.
    for user, expected_unread in ((scenario.genesis, 1), (scenario.additional, 0), (scenario.outsider, 0)):
        assert await unread_count(scenario, user) == expected_unread
        response = await scenario.client.post(
            f"/api/notifications/{welcome['id']}/read", headers=scenario.headers(user)
        )
        assert response.status_code == 200, response.text
        assert await unread_count(scenario, user) == expected_unread
        response = await scenario.client.post("/api/notifications/read-all", headers=scenario.headers(user))
        assert response.status_code == 200, response.text
        assert await unread_count(scenario, user) == 0

    # Then the applicant's private unread state has not changed.
    assert await unread_count(scenario, scenario.applicant) == 2
    assert all(not item["is_read"] for item in await notifications(scenario, scenario.applicant))


async def test_applicant_can_read_welcome_outside_existing_categories(admission_scenario: AdmissionScenario) -> None:
    # Given the applicant's welcome and admission-resolution notification.
    scenario = admission_scenario
    response = await approve(scenario, await scenario.submit())
    assert response.status_code == 200, response.text
    welcome = next(
        item for item in await notifications(scenario, scenario.applicant) if item["type"] == "organization_welcome"
    )
    for category, expected_types in (("approval", ["company_admission_resolved"]), ("broadcast", []), ("social", [])):
        response = await scenario.client.get(
            "/api/notifications", params={"category": category}, headers=scenario.headers(scenario.applicant)
        )
        assert response.status_code == 200, response.text
        assert [item["type"] for item in NOTIFICATIONS.validate_json(response.content)] == expected_types

    # When the applicant reads their welcome.
    response = await scenario.client.post(
        f"/api/notifications/{welcome['id']}/read", headers=scenario.headers(scenario.applicant)
    )

    # Then only the separate admission-resolution notification remains unread.
    assert response.status_code == 200, response.text
    assert await unread_count(scenario, scenario.applicant) == 1
    rows = await notifications(scenario, scenario.applicant)
    assert {item["type"]: item["is_read"] for item in rows} == {
        "organization_welcome": True,
        "company_admission_resolved": False,
    }


async def test_welcome_failure_rolls_back_approval(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a callback which writes the real welcome, then fails in the transaction.
    scenario = admission_scenario
    request_id = await scenario.submit()
    send_welcome = company_admission_decisions.send_organization_welcome
    failure = RuntimeError()

    async def fail_after_welcome(user: UserRecord, tenant: TenantRecord) -> None:
        await send_welcome(user, tenant)
        raise failure

    monkeypatch.setattr(company_admission_decisions, "send_organization_welcome", fail_after_welcome)

    # When approval encounters that failure.
    with pytest.raises(RuntimeError) as raised:
        await approve(scenario, request_id)

    # Then the real database retains the pending request and original membership.
    assert raised.value is failure
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0
    assert (
        await scenario.db.fetchval(
            "SELECT count(*) FROM notifications WHERE type IN ('organization_welcome','company_admission_resolved')"
        )
        == 0
    )
    assert await notifications(scenario, scenario.applicant) == []


async def test_registration_with_pending_invitation_sends_no_welcome(
    admission_scenario: AdmissionScenario,
) -> None:
    # Given an invitation to Acme and an account which does not exist yet.
    scenario = admission_scenario

    # When registration creates a default-organization membership pending review.
    response = await scenario.client.post(
        "/api/auth/register/init",
        json={"email": "newcomer@example.com", "password": "qa-password", "invitation_code": "JOIN"},
    )

    # Then neither organization gets a welcome before approval, including through its real token.
    assert response.status_code == 201, response.text
    registered = RegisterInitResponse.model_validate_json(response.content)
    assert registered.user.tenant_id == scenario.source_id
    assert registered.admission_request is not None
    assert registered.admission_request.status == "pending"
    assert (
        await scenario.db.fetchall(
            "SELECT user_id,agent_id,ref_id FROM notifications WHERE type='organization_welcome'"
        )
        == []
    )
    headers = {"Authorization": f"Bearer {registered.access_token}"}
    response = await scenario.client.get("/api/notifications", headers=headers)
    assert response.status_code == 200, response.text
    rows = NOTIFICATIONS.validate_json(response.content)
    assert rows == []
    response = await scenario.client.get("/api/notifications/unread-count", headers=headers)
    assert response.status_code == 200, response.text
    assert UNREAD_COUNT.validate_json(response.content)["unread_count"] == 0
