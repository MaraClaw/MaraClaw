"""Capacity orders against a real schema and the real HTTP stack: pricing, payment, review, audience and settings."""

import asyncio
import hashlib
import re
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.records.capacity_order import CapacityOrderStatus
from app.services import capacity_orders
from app.services.capacity_ledger import CapacityExtras
from app.services.company_capacity import company_caps
from capacity_orders_fixtures import (
    PDF_BYTES,
    PNG_BYTES,
    SETTINGS,
    CapacityScenario,
    capacity_scenario,
    detail,
    listed,
    payload,
)

__all__ = ["capacity_scenario"]

RECIPIENT = {"bank_name": "Vietcombank", "account_number": "0011 0022 3344", "branch_name": "Sai Gon"}


async def _tenant_files(scenario: CapacityScenario) -> list[str]:
    return [str(path.relative_to(scenario.storage_root)) for path in scenario.stored_files()]


# ── purchase: server-side price, snapshots, eligibility ─────────────────


async def test_a_purchase_is_priced_by_the_server_and_locks_quote_rate_and_recipient(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()

    response = await s.order("oa", accounts=2, agents=3)

    assert response.status_code == 201, response.text
    order = payload(response)
    # Nov 14 has 17 of 30 days left; 2 x $5 + 3 x $35 = $115/month; 115 * 17 / 30 = $65.1667.
    assert order["status"] == "awaiting_payment"
    assert order["kind"] == "purchase"
    assert order["tier"] == "pro"
    assert order["billing_month"] == "2026-11-01"
    assert (order["days_charged"], order["days_in_month"]) == (17, 30)
    assert (order["extra_accounts"], order["extra_agents"]) == (2, 3)
    assert (order["account_unit_usd"], order["agent_unit_usd"]) == (5, 35)
    assert order["amount_usd_cents"] == 6_517
    assert order["usd_vnd_rate"] == 25_000
    assert order["amount_vnd"] == 1_630_000
    assert order["recipient"] == RECIPIENT
    assert order["grace_until"] is None
    assert order["evidence"] is None
    assert order["created_by"] == str(s["oa"].id)
    assert order["tenant_id"] == str(s.tenant_id)
    assert order["tenant_name"] == "Acme"
    assert re.fullmatch(r"MC[A-Z2-7]{8}", str(order["reference"]))

    # A later rate or bank change never touches an order that was already quoted.
    await s.configure(usd_vnd_rate=30_000, bank_name="ACB", account_number="999")
    again = payload(await s.call("oa", "GET", f"/capacity/orders/{order['id']}"))
    assert (again["amount_vnd"], again["usd_vnd_rate"], again["recipient"]) == (1_630_000, 25_000, RECIPIENT)


async def test_the_client_never_sets_a_price(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()

    response = await s.call("oa", "POST", "/capacity/orders", json={"extra_accounts": 1, "amount_vnd": 1})

    assert response.status_code == 422
    assert await s.statuses() == []


async def test_a_purchase_needs_configured_payments(capacity_scenario: CapacityScenario):
    s = capacity_scenario

    unset = await s.order("oa", accounts=1)

    assert unset.status_code == 409
    assert detail(unset) == {
        "code": "payments_not_configured",
        "message": "Payments are not configured yet. Contact the platform admin.",
    }
    await s.db.execute(
        "INSERT INTO system_settings (key, value) VALUES ('capacity_payment_settings', %(v)s)",
        {"v": '{"bank_name": "Only a name"}'},
    )
    assert detail(await s.order("oa", accounts=1))["code"] == "payments_not_configured"
    assert await s.statuses() == []


@pytest.mark.parametrize("who", ["legacy_oa", "system_oa"])
async def test_only_tiered_non_system_companies_can_buy(capacity_scenario: CapacityScenario, who: str):
    s = capacity_scenario
    await s.configure()

    response = await s.order(who, accounts=1)

    assert response.status_code == 409
    assert detail(response)["code"] == "not_eligible"


@pytest.mark.parametrize("who", ["genesis_pa", "extra_pa", "member", "agent_admin"])
async def test_only_an_org_admin_may_buy(capacity_scenario: CapacityScenario, who: str):
    s = capacity_scenario
    await s.configure()

    assert (await s.order(who, accounts=1)).status_code == 403
    assert await s.statuses() == []


async def test_a_company_has_at_most_one_open_purchase_until_it_is_cancelled(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    first = await s.new_order(accounts=1)

    second = await s.order("oa2", agents=1)

    assert second.status_code == 409
    assert detail(second) == {
        "code": "purchase_already_open",
        "message": "Finish or cancel the open purchase before starting another",
        "order_id": first["id"],
    }
    assert (await s.call("oa", "POST", f"/capacity/orders/{first['id']}/cancel")).status_code == 200
    assert (await s.order("oa2", agents=1)).status_code == 201
    # Another company is unaffected by this one's open purchase.
    assert (await s.order("other_oa", accounts=1)).status_code == 201


async def test_concurrent_purchases_of_one_company_open_exactly_one_order(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()

    results = await asyncio.gather(*(s.order(who, accounts=1) for who in ("oa", "oa2", "oa", "oa2")))

    assert sorted(result.status_code for result in results) == [201, 409, 409, 409]
    assert len(await s.statuses()) == 1


# ── payment evidence ────────────────────────────────────────────────────


async def test_evidence_is_sniffed_stored_outside_agent_space_and_sends_the_order_for_review(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)

    # The client calls a PNG a PDF and names it like an executable: the bytes decide.
    response = await s.pay(order["id"], data=PNG_BYTES, name="..\\..\\receipt.exe", content_type="application/pdf")

    assert response.status_code == 200, response.text
    body = payload(response)
    assert body["status"] == "submitted"
    assert body["submitted_by"] == str(s["oa"].id)
    assert body["submitted_at"] is not None
    assert body["evidence"] == {
        "content_type": "image/png",
        "filename": "receipt.png",
        "size_bytes": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
    }
    assert "storage_key" not in response.text
    files = await _tenant_files(s)
    assert len(files) == 1
    assert re.fullmatch(rf"_capacity_payments/{s.tenant_id}/{order['id']}/[0-9a-f-]{{36}}\.png", files[0])
    assert s.stored_files()[0].read_bytes() == PNG_BYTES
    row = await s.order_row(order["id"])
    assert str(row["evidence_storage_key"]) == files[0]


async def test_a_pdf_is_accepted_and_every_platform_admin_is_told_exactly_once(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(agents=1)

    assert (
        await s.pay(order["id"], data=PDF_BYTES, name="transfer.pdf", content_type="application/pdf")
    ).status_code == 200

    for admin in ("genesis_pa", "extra_pa"):
        (note,) = await s.notifications(admin, "capacity_payment_submitted")
        assert note["link"] == f"/capacity-payments/{order['id']}"
        assert str(note["ref_id"]) == order["id"]
        assert "Acme" in str(note["body"])
        assert str(order["reference"]) in str(note["body"])
    for quiet in ("oa", "oa2", "member", "other_oa"):
        assert await s.notifications(quiet, "capacity_payment_submitted") == []


@pytest.mark.parametrize(
    ("data", "status"),
    [
        (b"<html><script>alert(1)</script></html>", 415),
        (b"GIF89a" + b"\x00" * 32, 415),
        (b"", 415),
        (PNG_BYTES + b"\x00" * (10 * 1024 * 1024), 413),
    ],
)
async def test_unacceptable_evidence_is_refused_and_leaves_the_order_open(
    capacity_scenario: CapacityScenario, data: bytes, status: int
):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)

    response = await s.pay(order["id"], data=data, name="proof.png", content_type="image/png")

    assert response.status_code == status
    assert await _tenant_files(s) == []
    assert (await s.order_row(order["id"]))["status"] == "awaiting_payment"
    for admin in ("genesis_pa", "extra_pa"):
        assert await s.notifications(admin) == []


async def test_evidence_is_accepted_only_once_and_never_for_a_closed_order(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)
    assert (await s.pay(order["id"])).status_code == 200

    again = await s.pay(order["id"], who="oa2")

    assert again.status_code == 409
    assert detail(again)["code"] == "order_not_awaiting_payment"
    assert len(await _tenant_files(s)) == 1
    cancelled = await s.new_order(who="other_oa", accounts=1)
    assert (await s.call("other_oa", "POST", f"/capacity/orders/{cancelled['id']}/cancel")).status_code == 200
    assert (await s.pay(cancelled["id"], who="other_oa")).status_code == 409
    assert (await s.call("oa", "POST", f"/capacity/orders/{order['id']}/cancel")).status_code == 409


async def test_two_org_admins_paying_at_once_keep_one_evidence_file(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)

    results = await asyncio.gather(s.pay(order["id"], who="oa"), s.pay(order["id"], who="oa2"))

    assert sorted(result.status_code for result in results) == [200, 409]
    assert len(await _tenant_files(s)) == 1
    assert (await s.order_row(order["id"]))["status"] == "submitted"


async def test_a_lost_race_after_the_upload_discards_the_unused_file(
    capacity_scenario: CapacityScenario, monkeypatch: pytest.MonkeyPatch
):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)
    monkeypatch.setattr(capacity_orders.orders, "mark_submitted", AsyncMock(return_value=None))

    response = await s.pay(order["id"])

    assert response.status_code == 409
    assert await _tenant_files(s) == []
    assert (await s.order_row(order["id"]))["status"] == "awaiting_payment"


async def test_another_company_and_non_admins_cannot_pay_or_cancel(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)

    for who, status in (("other_oa", 404), ("genesis_pa", 403), ("member", 403), ("agent_admin", 403)):
        assert (await s.pay(order["id"], who=who)).status_code == status, who
        assert (await s.call(who, "POST", f"/capacity/orders/{order['id']}/cancel")).status_code == status, who
    assert await _tenant_files(s) == []
    assert (await s.order_row(order["id"]))["status"] == "awaiting_payment"


async def test_a_purchase_can_be_cancelled_twice_but_a_renewal_never(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)

    first = await s.call("oa", "POST", f"/capacity/orders/{order['id']}/cancel")
    second = await s.call("oa2", "POST", f"/capacity/orders/{order['id']}/cancel")

    assert (first.status_code, payload(first)["status"]) == (200, "cancelled")
    assert (second.status_code, payload(second)["status"]) == (200, "cancelled")
    assert await s.extras() == CapacityExtras(0, 0)
    renewal = await s.insert_order(
        kind="renewal", status="awaiting_payment", month=s.clock.today.replace(day=1), grace_until=s.clock.today
    )
    refused = await s.call("oa", "POST", f"/capacity/orders/{renewal}/cancel")
    assert refused.status_code == 409
    assert detail(refused)["code"] == "order_not_cancellable"


# ── review ──────────────────────────────────────────────────────────────


async def test_approval_raises_the_effective_extras_and_the_company_caps(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=2, agents=3)
    assert (await s.pay(order["id"])).status_code == 200
    assert await s.extras() == CapacityExtras(0, 0)
    before = payload(await s.call("oa", "GET", "/capacity/overview"))
    assert (before["account_limit"], before["agent_limit"]) == (5, 2)

    response = await s.decide(order["id"], "approve", who="extra_pa")

    assert response.status_code == 200, response.text
    body = payload(response)
    assert body["status"] == "approved"
    assert body["reviewed_by"] == str(s["extra_pa"].id)
    assert body["reviewed_at"] is not None
    assert body["review_note"] is None
    assert await s.extras() == CapacityExtras(2, 3)
    caps = await company_caps(s.db, s.tenant_id)
    assert caps is not None
    assert (caps.account_limit, caps.agent_limit, caps.extras) == (7, 5, CapacityExtras(2, 3))
    after = payload(await s.call("oa", "GET", "/capacity/overview"))
    assert (after["account_limit"], after["agent_limit"]) == (7, 5)
    assert (after["extra_accounts"], after["extra_agents"]) == (2, 3)
    assert after["open_orders"] == []
    assert await s.extras(tenant="other") == CapacityExtras(0, 0)


async def test_a_decision_tells_the_submitter_and_every_org_admin_once_and_nobody_else(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)
    assert (await s.pay(order["id"], who="oa2")).status_code == 200

    assert (await s.decide(order["id"])).status_code == 200

    for who in ("oa", "oa2"):
        (note,) = await s.notifications(who, "capacity_payment_approved")
        assert note["link"] == f"/capacity-orders/{order['id']}"
        assert str(note["ref_id"]) == order["id"]
        assert str(order["reference"]) in str(note["body"])
    for quiet in ("member", "agent_admin", "other_oa", "genesis_pa", "extra_pa"):
        assert await s.notifications(quiet, "capacity_payment_approved") == []
        assert await s.notifications(quiet, "capacity_payment_rejected") == []


async def test_a_rejection_needs_a_note_changes_nothing_and_is_told_with_its_reason(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=2)
    assert (await s.pay(order["id"])).status_code == 200

    assert (await s.decide(order["id"], "reject")).status_code == 422
    assert (await s.decide(order["id"], "reject", note="   ")).status_code == 422
    response = await s.decide(order["id"], "reject", note="Amount does not match the transfer")

    assert response.status_code == 200
    body = payload(response)
    assert (body["status"], body["review_note"]) == ("rejected", "Amount does not match the transfer")
    assert await s.extras() == CapacityExtras(0, 0)
    (note,) = await s.notifications("oa", "capacity_payment_rejected")
    assert "Amount does not match the transfer" in str(note["body"])
    assert len(await s.notifications("oa2", "capacity_payment_rejected")) == 1
    # A rejected purchase frees the company to buy again.
    assert (await s.order("oa", accounts=1)).status_code == 201


async def test_the_same_decision_replays_a_different_one_conflicts_and_only_submitted_orders_are_reviewed(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)

    early = await s.decide(order["id"])
    assert early.status_code == 409
    assert detail(early)["code"] == "order_not_submitted"
    assert (await s.pay(order["id"])).status_code == 200

    first = await s.decide(order["id"], "approve")
    replay = await s.decide(order["id"], "approve", who="extra_pa")
    opposite = await s.decide(order["id"], "reject", note="changed my mind")

    assert (first.status_code, replay.status_code, opposite.status_code) == (200, 200, 409)
    assert payload(replay)["reviewed_by"] == payload(first)["reviewed_by"] == str(s["genesis_pa"].id)
    assert detail(opposite)["code"] == "order_already_decided"
    assert len(await s.notifications("oa", "capacity_payment_approved")) == 1
    assert await s.extras() == CapacityExtras(1, 0)
    assert (await s.decide(uuid4())).status_code == 404


async def test_concurrent_conflicting_decisions_leave_one_outcome_and_one_set_of_notifications(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)
    assert (await s.pay(order["id"])).status_code == 200

    results = await asyncio.gather(
        s.decide(order["id"], "approve"),
        s.decide(order["id"], "reject", note="no", who="extra_pa"),
        s.decide(order["id"], "approve", who="extra_pa"),
    )

    final = str((await s.order_row(order["id"]))["status"])
    assert final in (CapacityOrderStatus.APPROVED.value, CapacityOrderStatus.REJECTED.value)
    codes = sorted(result.status_code for result in results)
    assert codes in ([200, 200, 409], [200, 409, 409])
    notices = len(await s.notifications("oa", "capacity_payment_approved")) + len(
        await s.notifications("oa", "capacity_payment_rejected")
    )
    assert notices == 1
    expected = CapacityExtras(1, 0) if final == "approved" else CapacityExtras(0, 0)
    assert await s.extras() == expected


@pytest.mark.parametrize("who", ["oa", "oa2", "member", "agent_admin", "other_oa"])
async def test_only_platform_admins_decide(capacity_scenario: CapacityScenario, who: str):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)
    assert (await s.pay(order["id"])).status_code == 200

    assert (await s.decide(order["id"], who=who)).status_code == 403
    assert (await s.order_row(order["id"]))["status"] == "submitted"


async def test_every_step_is_audited(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    first = await s.new_order(accounts=1)
    assert (await s.call("oa", "POST", f"/capacity/orders/{first['id']}/cancel")).status_code == 200
    second = await s.new_order(accounts=1)
    assert (await s.pay(second["id"])).status_code == 200
    assert (await s.decide(second["id"], "approve")).status_code == 200
    third = await s.new_order(agents=1)
    assert (await s.pay(third["id"])).status_code == 200
    assert (await s.decide(third["id"], "reject", note="blurry")).status_code == 200

    actions = await s.audit_actions()

    assert actions == [
        "capacity_payment_settings_update",
        "capacity_order_create",
        "capacity_order_cancel",
        "capacity_order_create",
        "capacity_order_submit",
        "capacity_order_approve",
        "capacity_order_create",
        "capacity_order_submit",
        "capacity_order_reject",
    ]
    row = await s.db.fetchone(
        "SELECT actor_id, tenant_id, target_type, target_id, changes, details FROM admin_audit_logs "
        "WHERE action = 'capacity_order_approve'"
    )
    assert row is not None
    assert (row["actor_id"], row["tenant_id"], row["target_id"]) == (
        s["genesis_pa"].id,
        s.tenant_id,
        UUID(str(second["id"])),
    )
    assert row["changes"] == {"status": {"before": "submitted", "after": "approved"}}
    assert isinstance(row["details"], dict)
    assert row["details"]["reference"] == second["reference"]


# ── audience: lists, details, overview ──────────────────────────────────


async def test_org_admins_list_only_their_company_newest_first_and_platform_admins_must_name_one(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    older = await s.purchase(accounts=1, approve=False)
    newer = await s.purchase(agents=1, approve=False)
    theirs = await s.new_order(accounts=1, who="other_oa")

    page = await s.call("oa2", "GET", "/capacity/orders")
    short = await s.call("oa", "GET", "/capacity/orders?limit=1")
    rest = await s.call("oa", "GET", "/capacity/orders?limit=1&offset=1")

    assert page.status_code == 200
    assert [item["id"] for item in listed(page)] == [newer["id"], older["id"]]
    assert payload(page)["has_more"] is False
    assert ([item["id"] for item in listed(short)], payload(short)["has_more"]) == ([newer["id"]], True)
    assert ([item["id"] for item in listed(rest)], payload(rest)["has_more"]) == ([older["id"]], False)
    assert (await s.call("genesis_pa", "GET", "/capacity/orders")).status_code == 400
    named = await s.call("extra_pa", "GET", f"/capacity/orders?tenant_id={s.tenants['other']}")
    assert [item["id"] for item in listed(named)] == [theirs["id"]]
    assert (await s.call("oa", "GET", f"/capacity/orders?tenant_id={s.tenants['other']}")).status_code == 403
    assert (await s.call("member", "GET", "/capacity/orders")).status_code == 403
    assert (await s.call("oa", "GET", "/capacity/orders?limit=0")).status_code == 422


async def test_an_order_is_visible_to_its_company_admins_and_platform_admins_only(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)
    path = f"/capacity/orders/{order['id']}"

    expected = {
        "oa": 200,
        "oa2": 200,
        "genesis_pa": 200,
        "extra_pa": 200,
        "other_oa": 404,
        "member": 403,
        "agent_admin": 403,
    }
    for who, status in expected.items():
        assert (await s.call(who, "GET", path)).status_code == status, who
    assert (await s.call("oa", "GET", f"/capacity/orders/{uuid4()}")).status_code == 404
    assert (await s.client.get(f"/api{path}")).status_code in (401, 403)
    body = payload(await s.call("oa", "GET", path))
    assert body["recipient"] == RECIPIENT
    assert body["reference"] == order["reference"]


async def test_the_review_queue_defaults_to_submitted_orders_with_the_company_name(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    paid = await s.new_order(accounts=1)
    assert (await s.pay(paid["id"])).status_code == 200
    open_order = await s.new_order(accounts=1, who="other_oa")

    queue = await s.call("extra_pa", "GET", "/admin/capacity-orders")
    awaiting = await s.call("extra_pa", "GET", "/admin/capacity-orders?status=awaiting_payment")

    assert [(item["id"], item["tenant_name"]) for item in listed(queue)] == [(paid["id"], "Acme")]
    assert [(item["id"], item["tenant_name"]) for item in listed(awaiting)] == [(open_order["id"], "Other")]
    assert (await s.call("extra_pa", "GET", f"/admin/capacity-orders/{paid['id']}")).status_code == 200
    for who in ("oa", "member", "agent_admin"):
        assert (await s.call(who, "GET", "/admin/capacity-orders")).status_code == 403, who
        assert (await s.call(who, "GET", f"/admin/capacity-orders/{paid['id']}")).status_code == 403, who
    assert (await s.call("extra_pa", "GET", "/admin/capacity-orders?status=bogus")).status_code == 422


async def test_the_overview_gives_the_users_page_everything_to_preview_the_same_quote(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario

    unset = payload(await s.call("oa", "GET", "/capacity/overview"))

    assert unset["capped"] is True
    assert unset["tier"] == "pro"
    assert (unset["included_accounts"], unset["included_agents"]) == (5, 2)
    assert (unset["account_limit"], unset["agent_limit"]) == (5, 2)
    assert (unset["accounts_used"], unset["agents_used"]) == (4, 0)
    assert (unset["account_unit_usd"], unset["agent_unit_usd"]) == (5, 35)
    assert (unset["today"], unset["days_in_month"], unset["remaining_days"]) == ("2026-11-14", 30, 17)
    assert (unset["payments_configured"], unset["usd_vnd_rate"]) == (False, None)
    assert (unset["open_orders"], unset["current_renewal"]) == ([], None)

    await s.configure()
    order = await s.new_order(accounts=1)
    configured = payload(await s.call("oa", "GET", "/capacity/overview"))

    assert (configured["payments_configured"], configured["usd_vnd_rate"]) == (True, 25_000)
    open_orders = configured["open_orders"]
    assert isinstance(open_orders, list)
    assert [{**item, "tenant_name": "Acme"} for item in open_orders] == [
        payload(await s.call("oa", "GET", f"/capacity/orders/{order['id']}"))
    ]


async def test_the_overview_is_read_only_for_platform_admins_and_uncapped_companies_say_so(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    tenant = f"?tenant_id={s.tenant_id}"

    assert (await s.call("genesis_pa", "GET", f"/capacity/overview{tenant}")).status_code == 200
    assert (await s.call("extra_pa", "GET", "/capacity/overview")).status_code == 400
    assert (await s.call("extra_pa", "GET", f"/capacity/overview?tenant_id={uuid4()}")).status_code == 404
    assert (await s.call("other_oa", "GET", f"/capacity/overview{tenant}")).status_code == 403
    assert (await s.call("member", "GET", "/capacity/overview")).status_code == 403
    assert (await s.call("agent_admin", "GET", "/capacity/overview")).status_code == 403
    assert (await s.client.get("/api/capacity/overview")).status_code in (401, 403)
    for who in ("legacy_oa", "system_oa"):
        body = payload(await s.call(who, "GET", "/capacity/overview"))
        assert body["capped"] is False
        assert (body["tier"], body["account_limit"], body["agent_limit"]) == (None, None, None)
        assert (body["extra_accounts"], body["extra_agents"]) == (0, 0)


# ── evidence download ───────────────────────────────────────────────────


async def test_evidence_download_is_served_sandboxed_to_platform_admins_and_the_companys_org_admins(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    order = await s.new_order(accounts=1)
    assert (await s.pay(order["id"], name="Biên lai.png")).status_code == 200
    path = f"/capacity/orders/{order['id']}/evidence"

    for who in ("oa", "oa2", "genesis_pa", "extra_pa"):
        response = await s.call(who, "GET", path)
        assert response.status_code == 200, who
        assert response.content == PNG_BYTES
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == "sandbox"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["content-disposition"].startswith('inline; filename="')
        assert "filename*=UTF-8''Bi%C3%AAn%20lai.png" in response.headers["content-disposition"]
    assert (await s.call("other_oa", "GET", path)).status_code == 404
    assert (await s.call("member", "GET", path)).status_code == 403
    assert (await s.call("agent_admin", "GET", path)).status_code == 403
    assert (await s.client.get(f"/api{path}")).status_code in (401, 403)


async def test_a_pdf_keeps_its_type_and_a_missing_file_or_evidence_is_a_404(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    unpaid = await s.new_order(accounts=1)
    assert (await s.call("oa", "GET", f"/capacity/orders/{unpaid['id']}/evidence")).status_code == 404
    assert (await s.pay(unpaid["id"], data=PDF_BYTES, name="x.png", content_type="image/png")).status_code == 200
    path = f"/capacity/orders/{unpaid['id']}/evidence"

    pdf = await s.call("genesis_pa", "GET", path)
    assert (pdf.headers["content-type"], pdf.content) == ("application/pdf", PDF_BYTES)
    s.stored_files()[0].unlink()
    assert (await s.call("genesis_pa", "GET", path)).status_code == 404


# ── payment settings ────────────────────────────────────────────────────


async def test_only_the_genesis_platform_admin_reads_and_writes_the_payment_settings(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    empty = await s.call("genesis_pa", "GET", "/admin/capacity-payment-settings")
    assert payload(empty) == {
        "configured": False,
        "bank_name": None,
        "account_number": None,
        "branch_name": None,
        "usd_vnd_rate": None,
    }

    saved = await s.call(
        "genesis_pa", "PUT", "/admin/capacity-payment-settings", json={**SETTINGS, "bank_name": "  Techcombank  "}
    )
    assert saved.status_code == 200
    assert payload(saved) == {**SETTINGS, "bank_name": "Techcombank", "configured": True}
    assert payload(await s.call("genesis_pa", "GET", "/admin/capacity-payment-settings"))["bank_name"] == "Techcombank"

    for who in ("extra_pa", "oa", "oa2", "member", "agent_admin", "other_oa"):
        assert (await s.call(who, "GET", "/admin/capacity-payment-settings")).status_code == 403, who
        assert (await s.call(who, "PUT", "/admin/capacity-payment-settings", json=SETTINGS)).status_code == 403, who
    assert (await s.client.get("/api/admin/capacity-payment-settings")).status_code in (401, 403)
    assert payload(await s.call("genesis_pa", "GET", "/admin/capacity-payment-settings"))["bank_name"] == "Techcombank"


async def test_a_settings_change_is_audited_with_only_what_changed(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    await s.configure(usd_vnd_rate=26_000)

    rows = await s.rows(
        "SELECT actor_id, target_type, changes FROM admin_audit_logs "
        "WHERE action = 'capacity_payment_settings_update' ORDER BY created_at, id"
    )

    assert len(rows) == 2
    assert {row["actor_id"] for row in rows} == {s["genesis_pa"].id}
    assert rows[0]["changes"] == {
        "bank_name": {"before": None, "after": SETTINGS["bank_name"]},
        "account_number": {"before": None, "after": SETTINGS["account_number"]},
        "branch_name": {"before": None, "after": SETTINGS["branch_name"]},
        "usd_vnd_rate": {"before": None, "after": 25_000},
    }
    assert rows[1]["changes"] == {"usd_vnd_rate": {"before": 25_000, "after": 26_000}}


async def test_the_generic_system_settings_endpoints_never_expose_or_change_the_bank_details(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    path = "/enterprise/system-settings/capacity_payment_settings"

    for who in ("member", "oa", "extra_pa", "genesis_pa"):
        read = await s.call(who, "GET", path)
        assert read.status_code == 404, who
        assert "Vietcombank" not in read.text
        write = await s.call(who, "PUT", path, json={"value": {"bank_name": "Evil Bank", "usd_vnd_rate": 1}})
        assert write.status_code == 404, who
    stored = await s.db.fetchval("SELECT value FROM system_settings WHERE key = 'capacity_payment_settings'")
    assert isinstance(stored, dict)
    assert stored["bank_name"] == "Vietcombank"
    # The generic endpoint still serves every other key.
    assert (
        await s.call("oa", "PUT", "/enterprise/system-settings/notification_bar", json={"value": {"enabled": True}})
    ).status_code == 200
    assert payload(await s.call("member", "GET", "/enterprise/system-settings/notification_bar"))["value"] == {
        "enabled": True
    }


async def test_deleting_a_company_removes_its_orders_and_their_renewals(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    gone = await s.db.fetchval(
        "INSERT INTO tenants (id, name, slug) VALUES (gen_random_uuid(), 'Gone', 'gone') RETURNING id"
    )
    assert isinstance(gone, UUID)
    s.tenants["gone"] = gone
    source = await s.insert_order(status="approved", month=s.clock.today.replace(day=1), tenant="gone")
    _ = await s.insert_order(
        kind="renewal",
        status="awaiting_payment",
        month=s.clock.today.replace(day=1),
        grace_until=s.clock.today,
        source_order_id=source,
        tenant="gone",
    )

    await s.db.execute("DELETE FROM tenants WHERE id = %(id)s", {"id": gone})

    assert await s.rows("SELECT 1 FROM capacity_orders WHERE tenant_id = %(id)s", {"id": gone}) == []
