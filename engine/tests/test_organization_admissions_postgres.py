import hashlib
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import anyio
import pytest

from app.dao.identity_dao import identity_dao
from app.db.errors import CheckViolationError
from app.records.organization_plan import OrganizationBillingCycle, OrganizationPlanId
from app.schemas.organization_admission import OrganizationAdmissionQuoteRequest
from app.services import organization_admissions, organization_plans
from app.services.capacity_evidence import MAX_EVIDENCE_BYTES
from app.services.organization_admission_quote_token import mint_quote_token, verify_quote_token
from organization_admission_fixtures import PHONE, PNG_BYTES, OrganizationScenario, organization_scenario

__all__ = ["organization_scenario"]

REFERENCE = re.compile(r"MO[A-Z2-7]{8}")
CLAIM_DOMAIN = (
    "INSERT INTO tenant_email_domains (id, tenant_id, domain, is_default) "
    "SELECT gen_random_uuid(), id, 'claimed.example.com', true FROM tenants WHERE slug = 'acme'"
)


async def _counts(scenario: OrganizationScenario) -> dict[str, object]:
    row = await scenario.db.fetchone(
        "SELECT (SELECT count(*) FROM tenants) AS tenants, (SELECT count(*) FROM users) AS users, "
        "(SELECT count(*) FROM identities) AS identities, (SELECT count(*) FROM tenant_email_domains) AS domains"
    )
    assert row is not None
    return row


def _stored_files(scenario: OrganizationScenario) -> list[str]:
    return sorted(
        str(path.relative_to(scenario.storage_root)) for path in scenario.storage_root.rglob("*") if path.is_file()
    )


async def test_submission_persists_normalized_snapshot_and_notifies_genesis_once(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    before = await _counts(scenario)
    response = await scenario.submit("  Buyer@Example.COM ", "max", "  Acme  Robotics ", "+84 912 345 678")
    row = await scenario.db.fetchone("SELECT * FROM organization_admission_requests")
    assert row is not None
    assert response.status_code == 202
    assert response.json() == {"status": "pending", "reference": row["payment_reference"]}
    assert REFERENCE.fullmatch(str(row["payment_reference"]))
    assert (row["organization_name"], row["email"], row["phone"], row["tier"], row["status"]) == (
        "Acme  Robotics",
        "buyer@example.com",
        PHONE,
        "max",
        "pending",
    )
    assert (row["billing"], row["amount_usd"], row["usd_vnd_rate"], row["amount_vnd"]) == (
        "monthly",
        249,
        25000,
        6_225_000,
    )
    assert row["recipient"] == {
        "bank_name": "Vietcombank",
        "account_number": "0011 0022 3344",
        "branch_name": "Sai Gon",
    }
    key = str(row["evidence_storage_key"])
    assert key.startswith(f"_organization_admissions/{row['id']}/")
    assert key.endswith(".png")
    assert _stored_files(scenario) == [key]
    assert await scenario.storage.read_bytes_bounded(key, 1024) == PNG_BYTES
    assert (row["evidence_content_type"], row["evidence_filename"], row["evidence_size_bytes"]) == (
        "image/png",
        "proof.png",
        len(PNG_BYTES),
    )
    assert row["evidence_sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert scenario.budget.seen == ["127.0.0.1"]
    assert row["plan"] == {
        "id": "max",
        "name": "Max",
        "currency": "USD",
        "monthly_price_usd": 249,
        "annual_price_usd": 2490,
        "included_accounts": 15,
        "additional_account_monthly_usd": 4,
        "max_agents_per_account": 3,
        "included_agents": 5,
        "additional_agent_monthly_usd": 49,
        "storage_limit_bytes": 5368709120,
        "max_file_size_bytes": 52428800,
        "daily_messages_per_agent": 200,
        "guest_memory_bytes": 3221225472,
        "guest_millicpus": 2000,
        "guest_cpu_shares": 1536,
        "guest_pids_limit": 768,
        "workspace_disk_bytes": 5368709120,
        "storage_addon_bytes": 10737418240,
        "storage_addon_monthly_usd": 5,
        "workspace_disk_addon_bytes": 5368709120,
        "workspace_disk_addon_monthly_usd": 3,
    }
    notes = await scenario.db.fetchall("SELECT user_id,type,link,ref_id,body FROM notifications")
    assert notes == [
        {
            "user_id": scenario.genesis.id,
            "type": "organization_admission_pending",
            "link": "/organization-admissions",
            "ref_id": row["id"],
            "body": (
                f"Acme  Robotics (buyer@example.com) requested the Max plan (monthly) and sent payment evidence "
                f"for 6,225,000 VND, reference {row['payment_reference']}."
            ),
        }
    ]
    assert await _counts(scenario) == before


async def test_duplicate_pending_email_is_refused_and_preserves_original(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    assert (await scenario.submit("buyer@example.com", "pro", "First Name")).status_code == 202
    second = await scenario.submit("BUYER@example.com", "ultra", "Second Name")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "admission_already_pending"
    rows = await scenario.db.fetchall("SELECT tier,organization_name FROM organization_admission_requests")
    assert rows == [{"tier": "pro", "organization_name": "First Name"}]
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 1
    assert len(_stored_files(scenario)) == 1


async def test_parallel_duplicate_submissions_create_one_request_one_notification_and_one_evidence_file(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    codes: list[int] = []

    async def submit() -> None:
        codes.append((await scenario.submit()).status_code)

    async with anyio.create_task_group() as group:
        for _ in range(4):
            group.start_soon(submit)
    assert sorted(codes) == [202, 409, 409, 409]
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 1
    assert len(_stored_files(scenario)) == 1


async def test_notification_failure_rolls_back_request_and_retry_succeeds(
    organization_scenario: OrganizationScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = organization_scenario
    with monkeypatch.context() as patch:
        patch.setattr(organization_admissions, "send_notification", AsyncMock(side_effect=RuntimeError("injected")))
        with pytest.raises(RuntimeError, match="injected"):
            _ = await scenario.submit()
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert _stored_files(scenario) == []
    assert (await scenario.submit()).status_code == 202
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 1
    assert len(_stored_files(scenario)) == 1


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
    assert {item["plan"]["monthly_price_usd"] for item in first["items"] + second["items"]} == {79}
    assert {item["phone"] for item in first["items"] + second["items"]} == {PHONE}
    assert {(item["billing"], item["amount_usd"], item["amount_vnd"]) for item in first["items"] + second["items"]} == {
        ("monthly", 79, 1_975_000)
    }
    assert {item["evidence"]["filename"] for item in first["items"] + second["items"]} == {"proof.png"}
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


@pytest.mark.parametrize(
    ("tier", "billing", "usd"),
    [
        ("pro", "monthly", 79),
        ("pro", "annual", 790),
        ("max", "monthly", 249),
        ("max", "annual", 2490),
        ("ultra", "monthly", 699),
        ("ultra", "annual", 6990),
    ],
)
async def test_quote_prices_the_first_period_at_the_admin_rate_and_stores_nothing(
    organization_scenario: OrganizationScenario, tier: str, billing: str, usd: int
):
    scenario = organization_scenario
    response = await scenario.quote(" Buyer@Example.COM ", tier, "  Acme  Robotics ", "+84 912 345 678", billing)
    body = response.json()
    assert response.status_code == 200
    assert (body["organization_name"], body["email"], body["phone"], body["tier"]) == (
        "Acme  Robotics",
        "buyer@example.com",
        PHONE,
        tier,
    )
    assert (body["billing"], body["amount_usd"], body["usd_vnd_rate"], body["amount_vnd"]) == (
        billing,
        usd,
        25000,
        usd * 25000,
    )
    assert body["plan"]["id"] == tier
    assert body["recipient"] == {
        "bank_name": "Vietcombank",
        "account_number": "0011 0022 3344",
        "branch_name": "Sai Gon",
    }
    assert REFERENCE.fullmatch(body["reference"])
    assert body["quote_token"]
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 0
    assert _stored_files(scenario) == []


async def test_quote_rounds_the_vnd_amount_up_to_the_next_thousand(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    await scenario.db.execute(
        "UPDATE system_settings SET value = jsonb_set(value, '{usd_vnd_rate}', '24567') "
        "WHERE key = 'capacity_payment_settings'"
    )
    monthly = (await scenario.quote(tier="pro")).json()
    annual = (await scenario.quote(tier="pro", billing="annual")).json()
    assert (monthly["usd_vnd_rate"], monthly["amount_vnd"], annual["amount_vnd"]) == (24567, 1_941_000, 19_408_000)


async def test_quote_expires_seven_days_after_it_was_priced(organization_scenario: OrganizationScenario):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    request = OrganizationAdmissionQuoteRequest(
        organization_name="Acme",
        email="buyer@example.com",
        phone=PHONE,
        tier=OrganizationPlanId.PRO,
        billing=OrganizationBillingCycle.MONTHLY,
    )
    quote = await organization_admissions.quote_request(request, now=now)
    assert quote.expires_at == now + timedelta(days=7)


async def test_quote_is_refused_when_payments_are_not_configured(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    await scenario.db.execute("DELETE FROM system_settings WHERE key = 'capacity_payment_settings'")
    response = await scenario.quote()
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "payments_not_configured",
        "message": "Organization requests are temporarily unavailable. Try again later.",
    }


async def test_quote_is_refused_for_an_email_that_already_has_an_account(organization_scenario: OrganizationScenario):
    _ = await identity_dao.create(obj_in={"email": "taken@example.com", "email_verified": True})
    response = await organization_scenario.quote(email="Taken@Example.COM")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "email_registered"


async def test_quote_is_refused_for_a_domain_another_company_claimed(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    await scenario.db.execute(CLAIM_DOMAIN)
    response = await scenario.quote(email="someone@claimed.example.com")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "domain_claimed"


async def test_quote_is_refused_without_an_active_genesis_platform_admin(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    await scenario.db.execute("UPDATE users SET is_active=false WHERE id=%(id)s", {"id": scenario.genesis.id})
    assert (await scenario.quote()).status_code == 503


async def test_submission_is_refused_when_the_domain_was_claimed_after_the_quote(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    quoted = (await scenario.quote(email="late@claimed.example.com")).json()
    await scenario.db.execute(CLAIM_DOMAIN)
    response = await scenario.send_evidence(quoted["quote_token"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "domain_claimed"
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert _stored_files(scenario) == []


async def test_submission_is_refused_when_the_email_registered_after_the_quote(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    quoted = (await scenario.quote(email="late@example.com")).json()
    _ = await identity_dao.create(obj_in={"email": "late@example.com", "email_verified": True})
    response = await scenario.send_evidence(quoted["quote_token"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "email_registered"
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert _stored_files(scenario) == []


async def test_submission_is_refused_when_the_genesis_platform_admin_is_gone_after_the_quote(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    quoted = (await scenario.quote()).json()
    await scenario.db.execute("UPDATE users SET is_genesis=false WHERE id=%(id)s", {"id": scenario.genesis.id})
    assert (await scenario.send_evidence(quoted["quote_token"])).status_code == 503
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert _stored_files(scenario) == []


async def test_oversized_evidence_is_refused_before_anything_is_stored(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    quoted = (await scenario.quote()).json()
    response = await scenario.send_evidence(quoted["quote_token"], PNG_BYTES + b"\x00" * MAX_EVIDENCE_BYTES)
    assert (response.status_code, response.json()["detail"]) == (413, "Payment evidence must be 10 MiB or smaller")
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert (_stored_files(scenario), scenario.budget.seen) == ([], [])


async def test_evidence_that_is_not_an_image_or_pdf_is_refused_whatever_its_declared_type(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    quoted = (await scenario.quote()).json()
    response = await scenario.send_evidence(quoted["quote_token"], b"<html>proof</html>", "proof.png", "image/png")
    assert response.status_code == 415
    assert response.json()["detail"] == "Payment evidence must be a PNG, JPEG or WebP image or a PDF"
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert (_stored_files(scenario), scenario.budget.seen) == ([], [])


async def test_an_expired_quote_is_refused_with_410_and_nothing_is_stored(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    token = (await scenario.quote()).json()["quote_token"]
    terms = verify_quote_token(token, now=datetime.now(UTC))
    expired = mint_quote_token(terms.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}))
    response = await scenario.send_evidence(expired)
    assert (response.status_code, response.json()["detail"]) == (
        410,
        {"code": "quote_expired", "message": "This price quote has expired. Start again from the pricing page."},
    )
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert (_stored_files(scenario), scenario.budget.seen) == ([], [])


async def test_a_quote_whose_price_was_edited_is_refused_with_400(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    token = (await scenario.quote()).json()["quote_token"]
    terms = verify_quote_token(token, now=datetime.now(UTC))
    forged_payload = mint_quote_token(terms.model_copy(update={"amount_vnd": 1000})).partition(".")[0]
    for forged in (f"{forged_payload}.{token.partition('.')[2]}", "not-a-token", token.partition(".")[0]):
        response = await scenario.send_evidence(forged)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "quote_invalid"
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert (_stored_files(scenario), scenario.budget.seen) == ([], [])


async def test_a_client_over_its_budget_is_refused_with_429_and_nothing_is_stored(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    scenario.budget.allowed = False
    response = await scenario.submit()
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "too_many_requests"
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 0
    assert (_stored_files(scenario), scenario.budget.seen) == ([], ["127.0.0.1"])


async def test_a_quote_cannot_be_used_twice_and_the_second_evidence_is_discarded(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    token = (await scenario.quote()).json()["quote_token"]
    assert (await scenario.send_evidence(token)).status_code == 202
    assert (await scenario.send_evidence(token)).json()["detail"]["code"] == "admission_already_pending"
    rejected = await scenario.decide(await scenario.pending_id(), scenario.genesis, {"decision": "reject"})
    assert rejected.status_code == 200
    again = await scenario.send_evidence(token)
    assert (again.status_code, again.json()["detail"]) == (
        409,
        {"code": "quote_already_used", "message": "This request was already submitted."},
    )
    assert await scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests") == 1
    assert len(_stored_files(scenario)) == 1


async def test_genesis_platform_admin_downloads_the_evidence_with_hardened_headers(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    _ = await scenario.submit()
    response = await scenario.evidence(await scenario.pending_id(), scenario.genesis)
    assert response.status_code == 200
    assert response.content == PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].startswith('inline; filename="proof.png"')


@pytest.mark.parametrize("actor", ["additional", "org_genesis", "member"])
async def test_only_genesis_platform_admin_may_download_the_evidence(
    organization_scenario: OrganizationScenario, actor: str
):
    scenario = organization_scenario
    _ = await scenario.submit()
    request_id = await scenario.pending_id()
    assert (await scenario.evidence(request_id, getattr(scenario, actor))).status_code == 403
    assert (await scenario.evidence(request_id)).status_code == 401


async def test_evidence_of_an_unknown_request_or_a_missing_file_is_404(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    unknown = await scenario.evidence("00000000-0000-0000-0000-000000000001", scenario.genesis)
    assert unknown.status_code == 404
    _ = await scenario.submit()
    for path in scenario.storage_root.rglob("*"):
        if path.is_file():
            path.unlink()
    missing = await scenario.evidence(await scenario.pending_id(), scenario.genesis)
    assert (missing.status_code, missing.json()["detail"]) == (404, "The payment evidence file is missing")
