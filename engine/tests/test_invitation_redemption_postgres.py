from unittest.mock import AsyncMock

import anyio
import pytest
from fastapi import HTTPException

from app.db.session import connection_ctx
from app.services import company_admission_auto as auto
from company_admission_fixtures import AdmissionScenario, admission_scenario
from invitation_fixtures import join, new_member, put_code, unassign, used_count

__all__ = ["admission_scenario"]


async def transfer(scenario: AdmissionScenario, code: str = "JOIN", password: str | None = None, user=None):
    return await scenario.client.post(
        "/api/tenants/transfer",
        json={"invitation_code": code, "password": "qa-password" if password is None else password},
        headers=scenario.headers(user or scenario.applicant),
    )


async def tenant_of(scenario: AdmissionScenario, user):
    return await scenario.db.fetchval("SELECT tenant_id FROM users WHERE id=%(id)s", {"id": user.id})


async def test_valid_invitation_admits_a_newcomer_without_waiting_for_a_reviewer(admission_scenario: AdmissionScenario):
    # Given an unassigned newcomer with a valid code bound to their email.
    scenario = admission_scenario
    await unassign(scenario)

    # When the code is redeemed through /join.
    response = await join(scenario, "JOIN")

    # Then the newcomer is already in the company with an approved, reviewer-less admission.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert (body["request"]["status"], body["request"]["method"]) == ("approved", "invitation")
    assert body["request"]["source_tenant_id"] is None
    user = await scenario.current_applicant()
    assert (user.tenant_id, user.role, user.quota_max_agents) == (scenario.target_id, "member", 9)
    assert await scenario.db.fetchone("SELECT status,decided_by_user_id FROM company_admission_requests") == {
        "status": "approved",
        "decided_by_user_id": None,
    }
    code = await scenario.db.fetchone("SELECT used_count,redeemed_by FROM invitation_codes WHERE code='JOIN'")
    assert code == {"used_count": 1, "redeemed_by": scenario.applicant.id}
    notices = await scenario.db.fetchall("SELECT user_id,type FROM notifications")
    assert notices == [{"user_id": scenario.applicant.id, "type": "organization_welcome"}]


async def test_existing_member_cannot_use_the_password_free_join_endpoint(admission_scenario: AdmissionScenario):
    scenario = admission_scenario

    response = await join(scenario, "JOIN")

    assert response.status_code == 409
    assert await used_count(scenario) == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0


async def test_default_member_transfer_needs_the_password_then_admits(admission_scenario: AdmissionScenario):
    scenario = admission_scenario

    wrong = await transfer(scenario, password="nope")
    assert wrong.status_code == 401
    assert await used_count(scenario) == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id

    right = await transfer(scenario)
    assert right.status_code == 200, right.text
    assert right.json()["status"] == "approved"
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id
    assert await used_count(scenario) == 1


async def test_transfer_without_invitation_to_a_company_is_refused_even_with_the_password(
    admission_scenario: AdmissionScenario,
):
    scenario = admission_scenario

    response = await scenario.client.post(
        "/api/tenants/transfer",
        json={"password": "qa-password", "tenant_id": str(scenario.target_id)},
        headers=scenario.headers(scenario.applicant),
    )

    assert response.status_code == 403, response.text
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


@pytest.mark.parametrize("entrypoint", ["join", "transfer"])
async def test_replaying_a_spent_code_is_rejected(admission_scenario: AdmissionScenario, entrypoint: str):
    scenario = admission_scenario
    if entrypoint == "join":
        await unassign(scenario)
        first, replay = await join(scenario, "JOIN"), None
        assert first.status_code == 200
        replay = await join(scenario, "JOIN")
    else:
        assert (await transfer(scenario)).status_code == 200
        replay = await transfer(scenario)

    assert replay.status_code in {400, 409}
    assert await used_count(scenario) == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 1


@pytest.mark.parametrize("flaw", ["expired", "revoked", "other_recipient"])
async def test_unusable_code_never_moves_or_consumes(admission_scenario: AdmissionScenario, flaw: str):
    # Given a code that is unusable for exactly one reason.
    scenario = admission_scenario
    await put_code(
        scenario,
        "FLAWED",
        hours=-1 if flaw == "expired" else 1,
        is_active=flaw != "revoked",
        invited_email="someone.else@acme.test" if flaw == "other_recipient" else "applicant@acme.test",
    )

    # When it is redeemed with the correct password.
    response = await transfer(scenario, "FLAWED")

    # Then it fails with no admission, membership change or consumption.
    assert response.status_code == 400
    assert await used_count(scenario, "FLAWED") == 0
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


async def test_recipient_match_is_canonical_and_exclusive(admission_scenario: AdmissionScenario):
    # Given a code for applicant@acme.test, an account whose address differs only by case, and a stranger.
    scenario = admission_scenario
    stranger = await new_member(scenario, "stranger")
    await scenario.db.execute(
        "UPDATE identities SET email='Applicant@ACME.test' WHERE id=%(id)s", {"id": scenario.applicant.identity_id}
    )

    # When both redeem it.
    wrong = await transfer(scenario, user=stranger)
    right = await transfer(scenario)

    # Then only the recipient is admitted.
    assert (wrong.status_code, right.status_code) == (400, 200)
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id
    assert await tenant_of(scenario, stranger) == scenario.source_id


async def test_expiry_is_rechecked_when_a_pending_request_is_finally_approved(admission_scenario: AdmissionScenario):
    # Given a pending invitation request (registration path) whose code then expires.
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute("UPDATE invitation_codes SET expires_at=now() - interval '1 second'")

    # When the genesis administrator approves it.
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )

    # Then nothing moves and the code is not consumed.
    assert response.status_code == 409
    assert await used_count(scenario) == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


async def test_recipient_is_rechecked_when_a_pending_request_is_finally_approved(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute("UPDATE invitation_codes SET invited_email='someone.else@acme.test'")

    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )

    assert response.status_code == 409
    assert await used_count(scenario) == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


@pytest.mark.parametrize("entrypoint", ["join", "transfer"])
async def test_failure_after_the_move_rolls_back_membership_and_consumption(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, entrypoint: str
):
    scenario = admission_scenario
    monkeypatch.setattr(auto, "send_organization_welcome", AsyncMock(side_effect=RuntimeError("injected")))
    if entrypoint == "join":
        await unassign(scenario)

    with pytest.raises(RuntimeError, match="injected"):
        _ = await (join(scenario, "JOIN") if entrypoint == "join" else transfer(scenario))

    assert await used_count(scenario) == 0
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    assert (await scenario.current_applicant()).tenant_id == (None if entrypoint == "join" else scenario.source_id)


@pytest.mark.parametrize("drift", ["left_default_org", "became_admin", "source_no_longer_default"])
async def test_stale_applicant_snapshot_is_revalidated_under_locks(admission_scenario: AdmissionScenario, drift: str):
    # Given a principal loaded before the account changed.
    scenario = admission_scenario
    stale = await scenario.current_applicant()
    match drift:
        case "left_default_org":
            await scenario.db.execute("UPDATE users SET tenant_id=NULL WHERE id=%(id)s", {"id": stale.id})
        case "became_admin":
            await scenario.db.execute("UPDATE users SET role='org_admin' WHERE id=%(id)s", {"id": stale.id})
        case "source_no_longer_default":
            await scenario.db.execute(
                "UPDATE tenants SET is_default_end_user_org=false WHERE id=%(id)s", {"id": scenario.source_id}
            )

    # When the stale snapshot is used for redemption.
    with pytest.raises(HTTPException) as refused:
        async with connection_ctx():
            _ = await auto.admit_with_invitation(stale, scenario.target_id, "JOIN")

    # Then it is refused and nothing is consumed.
    assert refused.value.status_code in {403, 409}
    assert await used_count(scenario) == 0
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0


async def test_concurrent_redemptions_of_one_code_admit_exactly_once(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    statuses: list[int] = []

    async def redeem() -> None:
        statuses.append((await transfer(scenario)).status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(redeem)
        group.start_soon(redeem)

    assert min(statuses) == 200
    assert sorted(statuses)[1] in {400, 409}
    assert await used_count(scenario) == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 1
