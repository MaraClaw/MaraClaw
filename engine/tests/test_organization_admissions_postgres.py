from unittest.mock import AsyncMock

import anyio
import pytest

from app.db.errors import CheckViolationError
from app.services import organization_admissions, organization_plans
from organization_admission_fixtures import OrganizationScenario, organization_scenario

__all__ = ["organization_scenario"]

ACK = {"status": "pending"}


async def _counts(scenario: OrganizationScenario) -> dict[str, object]:
    row = await scenario.db.fetchone(
        "SELECT (SELECT count(*) FROM tenants) AS tenants, (SELECT count(*) FROM users) AS users, "
        "(SELECT count(*) FROM identities) AS identities, (SELECT count(*) FROM tenant_email_domains) AS domains"
    )
    assert row is not None
    return row


async def test_submission_persists_normalized_snapshot_and_notifies_genesis_once(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    before = await _counts(scenario)
    response = await scenario.submit("  Buyer@Example.COM ", "max", "  Acme  Robotics ")
    assert (response.status_code, response.json()) == (202, ACK)
    row = await scenario.db.fetchone("SELECT * FROM organization_admission_requests")
    assert row is not None
    assert (row["organization_name"], row["email"], row["tier"], row["status"]) == (
        "Acme  Robotics",
        "buyer@example.com",
        "max",
        "pending",
    )
    assert row["plan"] == {
        "id": "max",
        "name": "Max",
        "currency": "USD",
        "monthly_price_usd": 299,
        "included_accounts": 15,
        "additional_account_monthly_usd": 10,
        "max_accounts": 75,
        "max_agents_per_account": 5,
        "storage_limit_bytes": 5368709120,
        "max_file_size_bytes": 52428800,
    }
    notes = await scenario.db.fetchall("SELECT user_id,type,link,ref_id,body FROM notifications")
    assert notes == [
        {
            "user_id": scenario.genesis.id,
            "type": "organization_admission_pending",
            "link": "/organization-admissions",
            "ref_id": row["id"],
            "body": "Acme  Robotics (buyer@example.com) requested the Max plan.",
        }
    ]
    assert await _counts(scenario) == before


async def test_duplicate_pending_email_gets_same_ack_and_preserves_original(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    first = await scenario.submit("buyer@example.com", "pro", "First Name")
    second = await scenario.submit("BUYER@example.com", "ultra", "Second Name")
    assert (second.status_code, second.json()) == (first.status_code, first.json())
    rows = await scenario.db.fetchall("SELECT tier,organization_name FROM organization_admission_requests")
    assert rows == [{"tier": "pro", "organization_name": "First Name"}]
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 1


async def test_parallel_duplicate_submissions_create_one_request_and_one_notification(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    async with anyio.create_task_group() as group:
        for _ in range(4):
            group.start_soon(scenario.submit)
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 1


async def test_notification_failure_rolls_back_request_and_retry_succeeds(
    organization_scenario: OrganizationScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = organization_scenario
    with monkeypatch.context() as patch:
        patch.setattr(organization_admissions, "send_notification", AsyncMock(side_effect=RuntimeError("injected")))
        with pytest.raises(RuntimeError, match="injected"):
            _ = await scenario.submit()
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert (await scenario.submit()).status_code == 202
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 1


async def test_missing_genesis_platform_admin_fails_closed_without_persisting(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    await scenario.db.execute("UPDATE users SET is_genesis=false WHERE id=%(id)s", {"id": scenario.genesis.id})
    response = await scenario.submit()
    assert response.status_code == 503
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0


async def test_terminal_request_frees_the_email_for_a_new_pending_request(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    _ = await scenario.submit()
    rejected = await scenario.decide(await scenario.pending_id(), scenario.genesis, {"decision": "reject"})
    assert rejected.status_code == 200
    assert (await scenario.submit(tier="ultra")).status_code == 202
    rows = await scenario.db.fetchall("SELECT status,tier FROM organization_admission_requests ORDER BY created_at")
    assert rows == [{"status": "rejected", "tier": "pro"}, {"status": "pending", "tier": "ultra"}]
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 2


async def test_review_pages_by_status_and_snapshot_survives_catalog_change(
    organization_scenario: OrganizationScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = organization_scenario
    for number in range(3):
        _ = await scenario.submit(f"buyer{number}@example.com")
    monkeypatch.setattr(
        organization_plans,
        "ORGANIZATION_PLANS",
        tuple(plan.model_copy(update={"monthly_price_usd": 1}) for plan in organization_plans.ORGANIZATION_PLANS),
    )
    first = (await scenario.review(scenario.genesis, limit=2)).json()
    second = (await scenario.review(scenario.genesis, limit=2, offset=2)).json()
    assert [first["has_more"], second["has_more"]] == [True, False]
    assert [item["email"] for item in first["items"] + second["items"]] == [
        "buyer2@example.com",
        "buyer1@example.com",
        "buyer0@example.com",
    ]
    assert {item["plan"]["monthly_price_usd"] for item in first["items"] + second["items"]} == {99}
    assert (await scenario.review(scenario.genesis, status="approved")).json() == {"items": [], "has_more": False}


async def test_blank_note_is_stored_as_absent(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    _ = await scenario.submit()
    response = await scenario.decide(
        await scenario.pending_id(), scenario.genesis, {"decision": "reject", "note": "   "}
    )
    assert response.json()["review_note"] is None


async def test_concurrent_conflicting_decisions_yield_exactly_one_winner(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    _ = await scenario.submit()
    request_id = await scenario.pending_id()
    codes: list[int] = []

    async def decide(decision: str) -> None:
        codes.append((await scenario.decide(request_id, scenario.genesis, {"decision": decision})).status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(decide, "approve")
        group.start_soon(decide, "reject")
    assert sorted(codes) == [200, 409]
    assert await scenario.db.fetchval("SELECT status FROM organization_admission_requests") in {"approved", "rejected"}


@pytest.mark.parametrize("actor", ["additional", "org_genesis", "member"])
async def test_only_genesis_platform_admin_may_review(organization_scenario: OrganizationScenario, actor: str):
    scenario = organization_scenario
    _ = await scenario.submit()
    user = getattr(scenario, actor)
    assert (await scenario.review(user)).status_code == 403
    response = await scenario.decide(await scenario.pending_id(), user, {"decision": "approve"})
    assert response.status_code == 403
    assert await scenario.db.fetchval("SELECT status FROM organization_admission_requests") == "pending"


async def test_review_requires_authentication_and_fresh_identity(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    _ = await scenario.submit()
    request_id = await scenario.pending_id()
    anonymous = await scenario.client.post(
        f"/api/admin/organization-admissions/{request_id}/decision", json={"decision": "approve"}
    )
    assert anonymous.status_code == 401
    await scenario.db.execute(
        "UPDATE identities SET is_active=false WHERE id=%(id)s", {"id": scenario.genesis.identity_id}
    )
    assert (await scenario.review(scenario.genesis)).status_code == 401
    await scenario.db.execute(
        "UPDATE identities SET is_active=true, must_change_password=true WHERE id=%(id)s",
        {"id": scenario.genesis.identity_id},
    )
    assert (await scenario.decide(request_id, scenario.genesis, {"decision": "approve"})).status_code == 403
    assert await scenario.db.fetchval("SELECT status FROM organization_admission_requests") == "pending"


async def test_unknown_request_is_404_and_database_rejects_inconsistent_terminal_rows(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    missing = await scenario.decide("00000000-0000-0000-0000-000000000001", scenario.genesis, {"decision": "approve"})
    assert missing.status_code == 404
    _ = await scenario.submit()
    with pytest.raises(CheckViolationError):
        await scenario.db.execute("UPDATE organization_admission_requests SET status='approved'")
