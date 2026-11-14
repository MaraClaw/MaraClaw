"""The ledger, monthly renewal, expiry and late approval against a real schema (opt-in ``MARACLAW_TEST_POSTGRES_DSN``)."""

import asyncio
from datetime import date, timedelta
from uuid import UUID

import pytest

from app.db.errors import CheckViolationError, UniqueViolationError
from app.services import capacity_ledger, capacity_renewals
from app.services.capacity_ledger import CapacityExtras
from app.services.capacity_pricing import previous_month_start
from app.services.capacity_renewals import RenewalOutcome, RenewalPass, generate_primary_renewal, run_renewal_pass
from capacity_orders_fixtures import CapacityScenario, capacity_scenario, payload

__all__ = ["capacity_scenario"]

SEP, OCT, NOV, DEC, JAN = date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)


async def supplementary(
    s: CapacityScenario,
    *,
    status: str,
    month: date,
    accounts: int = 1,
    agents: int = 0,
    grace_until: date,
    tenant: str = "acme",
) -> UUID:
    """A supplementary renewal and the approved earlier-month order it renews."""
    source = await s.insert_order(status="approved", month=previous_month_start(month), tenant=tenant)
    return await s.insert_order(
        kind="renewal",
        status=status,
        month=month,
        accounts=accounts,
        agents=agents,
        grace_until=grace_until,
        source_order_id=source,
        tenant=tenant,
    )


async def renewals(s: CapacityScenario, month: date, tenant: str = "acme") -> list[dict[str, object]]:
    rows = await s.rows(
        "SELECT * FROM capacity_orders WHERE tenant_id = %(t)s AND kind = 'renewal' AND billing_month = %(m)s "
        "ORDER BY created_at, id",
        {"t": s.tenants[tenant], "m": month},
    )
    return [dict(row) for row in rows]


# ── what counts ─────────────────────────────────────────────────────────


async def test_only_approved_purchases_count(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    for status, accounts in (
        ("awaiting_payment", 1),
        ("submitted", 2),
        ("rejected", 4),
        ("cancelled", 8),
        ("expired", 16),
        ("approved", 32),
    ):
        _ = await s.insert_order(status=status, month=NOV, accounts=accounts, agents=accounts * 10)

    assert await s.extras(date(2026, 11, 1)) == CapacityExtras(32, 320)
    assert await s.extras(date(2026, 11, 30)) == CapacityExtras(32, 320)


async def test_a_renewal_counts_when_approved_or_submitted_or_while_unpaid_inside_its_grace(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    today = date(2026, 11, 14)
    _ = await s.insert_order(kind="renewal", status="approved", month=NOV, accounts=1, agents=10, grace_until=NOV)
    for status, accounts, grace in (
        ("submitted", 2, today - timedelta(days=30)),
        ("awaiting_payment", 4, today),
        ("awaiting_payment", 8, today - timedelta(days=1)),
        ("rejected", 16, today + timedelta(days=30)),
        ("expired", 32, today + timedelta(days=30)),
    ):
        _ = await supplementary(s, status=status, month=NOV, accounts=accounts, agents=accounts * 10, grace_until=grace)

    # Counted: approved 1, submitted 2, awaiting inside its grace (the last day counts) 4. Lapsed, rejected, expired: no.
    assert await s.extras(today) == CapacityExtras(7, 70)


@pytest.mark.parametrize(
    ("on_date", "counted"),
    [
        (date(2026, 12, 1), True),
        (date(2026, 12, 6), True),
        (date(2026, 12, 7), True),
        (date(2026, 12, 8), False),
        (date(2026, 12, 31), False),
    ],
)
async def test_an_unpaid_primary_renewal_counts_through_the_7th_and_lapses_on_the_8th(
    capacity_scenario: CapacityScenario, on_date: date, counted: bool
):
    s = capacity_scenario
    _ = await s.insert_order(
        kind="renewal", status="awaiting_payment", month=DEC, accounts=3, agents=1, grace_until=date(2026, 12, 7)
    )

    assert await s.extras(on_date) == (CapacityExtras(3, 1) if counted else CapacityExtras(0, 0))


async def test_before_the_primary_renewal_exists_the_previous_months_approved_coverage_stands_in(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    _ = await s.insert_order(status="approved", month=OCT, accounts=3, agents=2)
    _ = await s.insert_order(kind="renewal", status="approved", month=OCT, accounts=1, agents=0, grace_until=OCT)
    # Coverage is approved orders only, and only the previous month.
    _ = await s.insert_order(status="submitted", month=OCT, accounts=100)
    _ = await s.insert_order(status="approved", month=SEP, accounts=1000)
    _ = await s.insert_order(status="approved", month=OCT, accounts=7, tenant="other")
    assert await capacity_ledger.coverage(s.db, s.tenant_id, OCT) == CapacityExtras(4, 2)

    assert await s.extras(date(2026, 11, 20)) == CapacityExtras(4, 2)
    _ = await s.insert_order(status="approved", month=NOV, accounts=8)
    assert await s.extras(date(2026, 11, 20)) == CapacityExtras(12, 2)


async def test_once_the_primary_renewal_exists_it_replaces_the_fallback_and_is_not_double_counted(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    _ = await s.insert_order(status="approved", month=OCT, accounts=4, agents=2)
    _ = await s.insert_order(status="approved", month=NOV, accounts=8)
    _ = await s.insert_order(
        kind="renewal", status="awaiting_payment", month=NOV, accounts=4, agents=2, grace_until=date(2026, 11, 7)
    )

    assert await s.extras(date(2026, 11, 5)) == CapacityExtras(12, 2)
    assert await s.extras(date(2026, 11, 14)) == CapacityExtras(8, 0)


async def test_a_rejected_primary_renewal_still_ends_the_fallback(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    _ = await s.insert_order(status="approved", month=OCT, accounts=4)
    assert await s.extras(date(2026, 11, 3)) == CapacityExtras(4, 0)
    _ = await s.insert_order(
        kind="renewal", status="rejected", month=NOV, accounts=4, agents=0, grace_until=date(2026, 11, 7)
    )

    assert await s.extras(date(2026, 11, 3)) == CapacityExtras(0, 0)


async def test_one_companys_orders_never_count_for_another(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    _ = await s.insert_order(status="approved", month=NOV, accounts=5, tenant="other")

    assert await s.extras(date(2026, 11, 14)) == CapacityExtras(0, 0)
    assert await s.extras(date(2026, 11, 14), tenant="other") == CapacityExtras(5, 0)


# ── monthly renewal ─────────────────────────────────────────────────────


async def test_the_first_pass_of_a_month_renews_last_months_coverage_at_current_terms(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    _ = await s.purchase(accounts=2, agents=1)
    s.clock.today = DEC
    await s.configure(usd_vnd_rate=26_000, bank_name="ACB", account_number="777", branch_name="Ha Noi")
    # Before the worker runs, the company is not penalized: last month's coverage carries over.
    assert await s.extras() == CapacityExtras(2, 1)

    result = await run_renewal_pass()

    assert result == RenewalPass(created=1)
    (renewal,) = await renewals(s, DEC)
    assert renewal["status"] == "awaiting_payment"
    assert renewal["source_order_id"] is None
    assert renewal["created_by"] is None
    assert (renewal["tier"], renewal["billing_month"]) == ("pro", DEC)
    assert (renewal["extra_accounts"], renewal["extra_agents"]) == (2, 1)
    assert (renewal["account_unit_usd"], renewal["agent_unit_usd"]) == (5, 35)
    # A whole month, not prorated: $45 for 31 days at 26 000 VND.
    assert (renewal["days_charged"], renewal["days_in_month"]) == (31, 31)
    assert (renewal["amount_usd_cents"], renewal["usd_vnd_rate"], renewal["amount_vnd"]) == (4_500, 26_000, 1_170_000)
    assert renewal["recipient"] == {"bank_name": "ACB", "account_number": "777", "branch_name": "Ha Noi"}
    assert renewal["grace_until"] == date(2026, 12, 7)
    for day, expected in ((date(2026, 12, 1), True), (date(2026, 12, 7), True), (date(2026, 12, 8), False)):
        assert await s.extras(day) == (CapacityExtras(2, 1) if expected else CapacityExtras(0, 0))


async def test_the_company_admins_are_asked_to_pay_the_renewal_once(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    _ = await s.purchase(accounts=2, agents=1)
    s.clock.today = DEC

    _ = await run_renewal_pass()
    _ = await run_renewal_pass()

    (renewal,) = await renewals(s, DEC)
    for who in ("oa", "oa2"):
        (note,) = await s.notifications(who, "capacity_renewal_due")
        assert note["link"] == f"/capacity-orders/{renewal['id']}"
        assert str(note["ref_id"]) == str(renewal["id"])
        assert "1,125,000 VND" in str(note["body"])  # $45 for a whole month at 25 000
        assert "2026-12-07" in str(note["body"])
    for quiet in ("member", "agent_admin", "other_oa", "genesis_pa", "extra_pa"):
        assert await s.notifications(quiet, "capacity_renewal_due") == []


async def test_generation_is_idempotent_even_when_it_races(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    _ = await s.purchase(accounts=1)
    s.clock.today = DEC

    outcomes = await asyncio.gather(*(generate_primary_renewal(s.tenant_id, DEC) for _ in range(5)))

    assert sorted(outcomes) == sorted([RenewalOutcome.CREATED, *[RenewalOutcome.ALREADY_EXISTS] * 4])
    assert len(await renewals(s, DEC)) == 1
    assert len(await s.notifications("oa", "capacity_renewal_due")) == 1
    assert await run_renewal_pass() == RenewalPass()


async def test_a_paid_renewal_keeps_the_extras_and_rolls_into_the_next_month(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    _ = await s.purchase(accounts=2, agents=1)
    s.clock.today = DEC
    _ = await run_renewal_pass()
    (renewal,) = await renewals(s, DEC)
    assert (await s.pay(renewal["id"])).status_code == 200
    assert await s.extras(date(2026, 12, 20)) == CapacityExtras(2, 1)  # submitted counts while it is reviewed
    assert (await s.decide(renewal["id"])).status_code == 200
    assert await s.extras(date(2026, 12, 31)) == CapacityExtras(2, 1)

    s.clock.today = JAN
    result = await run_renewal_pass()

    assert result == RenewalPass(created=1)
    (next_renewal,) = await renewals(s, JAN)
    assert (next_renewal["extra_accounts"], next_renewal["extra_agents"]) == (2, 1)
    assert next_renewal["grace_until"] == date(2027, 1, 7)
    # Approval of a current-month order needs no supplementary renewal.
    assert len(await renewals(s, DEC)) == 1


async def test_an_unpaid_renewal_lapses_and_is_never_renewed_again(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    _ = await s.purchase(accounts=2, agents=1)
    s.clock.today = DEC
    _ = await run_renewal_pass()
    (renewal,) = await renewals(s, DEC)
    assert await s.extras(date(2026, 12, 20)) == CapacityExtras(0, 0)

    s.clock.today = JAN
    result = await run_renewal_pass()

    assert result == RenewalPass(expired=1)
    assert (await s.order_row(renewal["id"]))["status"] == "expired"
    assert await renewals(s, JAN) == []
    assert await s.extras() == CapacityExtras(0, 0)


async def test_a_renewal_covers_every_approved_order_of_the_previous_month(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    _ = await s.insert_order(status="approved", month=NOV, accounts=1, agents=0)
    _ = await s.insert_order(kind="renewal", status="approved", month=NOV, accounts=2, agents=1, grace_until=NOV)
    _ = await supplementary(s, status="approved", month=NOV, accounts=4, agents=0, grace_until=NOV)
    _ = await s.insert_order(status="submitted", month=NOV, accounts=1000)
    _ = await s.insert_order(status="rejected", month=NOV, accounts=1000)
    s.clock.today = DEC

    _ = await run_renewal_pass()

    (renewal,) = await renewals(s, DEC)
    assert (renewal["extra_accounts"], renewal["extra_agents"]) == (1 + 2 + 4, 1)


async def test_nothing_is_generated_without_approved_coverage_a_tier_or_when_the_company_is_a_system_one(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    _ = await s.insert_order(status="submitted", month=NOV, accounts=3)
    _ = await s.insert_order(status="rejected", month=NOV, accounts=3)
    _ = await s.insert_order(status="approved", month=NOV, accounts=3, tenant="legacy")
    _ = await s.insert_order(status="approved", month=NOV, accounts=3, tenant="system")

    outcomes = {
        tenant: await generate_primary_renewal(s.tenants[tenant], DEC) for tenant in ("acme", "legacy", "system")
    }

    assert outcomes == {
        "acme": RenewalOutcome.NOTHING_COVERED,
        "legacy": RenewalOutcome.NOT_ELIGIBLE,
        "system": RenewalOutcome.NOT_ELIGIBLE,
    }
    assert await s.rows("SELECT 1 FROM capacity_orders WHERE kind = 'renewal'") == []


async def test_unconfigured_payments_skip_the_renewal_and_a_later_pass_creates_it_with_a_fresh_grace(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    _ = await s.purchase(accounts=2, agents=1)
    await s.db.execute("DELETE FROM system_settings WHERE key = 'capacity_payment_settings'")
    s.clock.today = DEC

    skipped = await run_renewal_pass()

    assert skipped == RenewalPass(unconfigured=1)
    assert await renewals(s, DEC) == []
    # The company is not penalized for the platform's missing settings: the fallback keeps the extras.
    assert await s.extras(date(2026, 12, 20)) == CapacityExtras(2, 1)

    await s.configure()
    s.clock.today = date(2026, 12, 15)
    created = await run_renewal_pass()

    assert created == RenewalPass(created=1)
    (renewal,) = await renewals(s, DEC)
    assert renewal["grace_until"] == date(2026, 12, 21)  # seven days counting the 15th, later than the 7th
    assert await s.extras(date(2026, 12, 21)) == CapacityExtras(2, 1)
    assert await s.extras(date(2026, 12, 22)) == CapacityExtras(0, 0)


async def test_a_pass_pages_through_companies_and_one_failure_does_not_stop_the_rest(
    capacity_scenario: CapacityScenario, monkeypatch: pytest.MonkeyPatch
):
    s = capacity_scenario
    await s.configure()
    _ = await s.insert_order(status="approved", month=NOV, accounts=1, agents=1, tenant="acme")
    _ = await s.insert_order(status="approved", month=NOV, accounts=2, agents=2, tenant="other")
    monkeypatch.setattr(capacity_renewals, "TENANT_PAGE", 1)
    real = capacity_renewals.generate_primary_renewal

    async def flaky(tenant_id: UUID, today: date) -> RenewalOutcome:
        if tenant_id == s.tenant_id:
            raise RuntimeError("boom")
        return await real(tenant_id, today)

    monkeypatch.setattr(capacity_renewals, "generate_primary_renewal", flaky)
    s.clock.today = DEC

    first = await run_renewal_pass()

    assert first == RenewalPass(created=1, failed=1)
    assert await renewals(s, DEC) == []
    (other,) = await renewals(s, DEC, tenant="other")
    # The company's current tier prices (Max), not Pro's.
    assert (other["tier"], other["account_unit_usd"], other["agent_unit_usd"]) == ("max", 4, 49)
    assert (other["extra_accounts"], other["extra_agents"]) == (2, 2)
    assert other["amount_usd_cents"] == (2 * 4 + 2 * 49) * 100
    monkeypatch.setattr(capacity_renewals, "generate_primary_renewal", real)
    assert await run_renewal_pass() == RenewalPass(created=1)
    assert len(await renewals(s, DEC)) == 1


# ── expiry ──────────────────────────────────────────────────────────────


async def test_an_order_unpaid_past_its_month_expires_and_nothing_else_does(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    unpaid = await s.new_order(accounts=1)
    reviewed = await s.new_order(accounts=1, who="other_oa")
    assert (await s.pay(reviewed["id"], who="other_oa")).status_code == 200
    approved = await s.insert_order(status="approved", month=NOV, accounts=1)
    renewal = await supplementary(s, status="awaiting_payment", month=NOV, grace_until=date(2026, 11, 7))
    s.clock.today = DEC

    first = await run_renewal_pass()

    assert (first.expired, first.created) == (2, 1)
    assert (await s.order_row(unpaid["id"]))["status"] == "expired"
    assert (await s.order_row(renewal))["status"] == "expired"
    assert (await s.order_row(reviewed["id"]))["status"] == "submitted"  # still reviewable, never auto-expired
    assert (await s.order_row(approved))["status"] == "approved"
    assert (await s.pay(unpaid["id"])).status_code == 409
    assert len(s.stored_files()) == 1  # only the reviewed order's evidence was ever stored
    december = await s.new_order(accounts=1, who="oa2")
    assert december["billing_month"] == "2026-12-01"
    assert (await run_renewal_pass()).expired == 0  # a purchase of the current month is not stale
    assert (await s.order_row(december["id"]))["status"] == "awaiting_payment"


async def test_an_expired_purchase_frees_the_company_to_start_a_new_prorated_one(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    stale = await s.new_order(agents=1)
    s.clock.today = date(2026, 12, 17)
    assert (await s.order("oa", agents=1)).status_code == 409

    _ = await run_renewal_pass()
    fresh = await s.order("oa", agents=1)

    assert (await s.order_row(stale["id"]))["status"] == "expired"
    assert fresh.status_code == 201
    body = payload(fresh)
    assert (body["billing_month"], body["days_charged"], body["days_in_month"]) == ("2026-12-01", 15, 31)


async def test_expiry_works_through_batches(capacity_scenario: CapacityScenario, monkeypatch: pytest.MonkeyPatch):
    s = capacity_scenario
    for _ in range(5):
        _ = await supplementary(s, status="awaiting_payment", month=NOV, grace_until=date(2026, 11, 7))
    monkeypatch.setattr(capacity_renewals, "EXPIRY_BATCH", 2)

    assert await capacity_renewals.expire_stale_orders(DEC) == 5
    assert await capacity_renewals.expire_stale_orders(DEC) == 0
    assert await s.rows("SELECT 1 FROM capacity_orders WHERE status = 'awaiting_payment'") == []


# ── late approval ───────────────────────────────────────────────────────


async def _late_setup(s: CapacityScenario) -> tuple[dict[str, object], dict[str, object]]:
    """November: an approved purchase Q and a paid but unreviewed purchase P; then the December renewal exists."""
    await s.configure()
    q = await s.purchase(accounts=1)
    p = await s.purchase(accounts=2, agents=1, approve=False)
    s.clock.today = DEC
    _ = await run_renewal_pass()
    return q, p


async def test_approving_an_earlier_months_purchase_after_the_renewal_exists_adds_a_supplementary_renewal(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    _, p = await _late_setup(s)
    (primary,) = await renewals(s, DEC)
    assert (primary["extra_accounts"], primary["extra_agents"]) == (1, 0)  # P was not approved when it was generated
    s.clock.today = date(2026, 12, 3)
    await s.configure(usd_vnd_rate=27_000)

    response = await s.decide(p["id"], "approve")

    assert response.status_code == 200, response.text
    both = await renewals(s, DEC)
    (extra,) = [row for row in both if row["source_order_id"] is not None]
    assert str(extra["source_order_id"]) == p["id"]
    assert (extra["kind"], extra["status"], extra["billing_month"]) == ("renewal", "awaiting_payment", DEC)
    assert (extra["extra_accounts"], extra["extra_agents"]) == (2, 1)
    assert (extra["days_charged"], extra["days_in_month"]) == (31, 31)
    # A whole month at current terms: $45 at the new 27 000 rate, while P keeps the rate it was quoted at.
    assert (extra["amount_usd_cents"], extra["usd_vnd_rate"], extra["amount_vnd"]) == (4_500, 27_000, 1_215_000)
    assert extra["created_by"] is None
    assert extra["grace_until"] == date(2026, 12, 9)  # seven days counting the 3rd
    assert (await s.order_row(p["id"]))["usd_vnd_rate"] == 25_000
    for who in ("oa", "oa2"):
        notes = await s.notifications(who, "capacity_renewal_due")
        assert [str(note["ref_id"]) for note in notes] == [str(primary["id"]), str(extra["id"])]
    assert await s.extras(date(2026, 12, 3)) == CapacityExtras(3, 1)
    assert await s.extras(date(2026, 12, 8)) == CapacityExtras(2, 1)  # the primary lapsed, the supplementary has a day
    assert await s.extras(date(2026, 12, 9)) == CapacityExtras(2, 1)
    assert await s.extras(date(2026, 12, 10)) == CapacityExtras(0, 0)

    audit = await s.db.fetchone(
        "SELECT details FROM admin_audit_logs WHERE action = 'capacity_order_approve' AND target_id = %(id)s",
        {"id": UUID(str(p["id"]))},
    )
    assert audit is not None
    assert isinstance(audit["details"], dict)
    assert audit["details"]["supplementary_renewal_id"] == str(extra["id"])
    replay = await s.decide(p["id"], "approve")
    assert replay.status_code == 200
    assert len(await renewals(s, DEC)) == 2


async def test_late_approval_before_the_renewal_exists_adds_nothing_and_generation_covers_the_order(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    _ = await s.purchase(accounts=1)
    p = await s.purchase(accounts=2, agents=1, approve=False)
    s.clock.today = date(2026, 12, 3)

    assert (await s.decide(p["id"], "approve")).status_code == 200

    assert await renewals(s, DEC) == []
    assert await s.extras() == CapacityExtras(3, 1)  # the fallback counts both November purchases
    _ = await run_renewal_pass()
    (renewal,) = await renewals(s, DEC)
    assert (renewal["extra_accounts"], renewal["extra_agents"]) == (3, 1)
    assert renewal["grace_until"] == date(2026, 12, 9)  # created on the 3rd: seven days counting it, later than the 7th


async def _two_month_old_purchase(s: CapacityScenario) -> dict[str, object]:
    """An October purchase paid on the 30th, still unreviewed while the worker passes November and December 1st."""
    await s.configure()
    s.clock.today = date(2026, 10, 30)
    p = await s.purchase(accounts=2, agents=1, approve=False)
    for day in (NOV, DEC):
        s.clock.today = day
        assert await run_renewal_pass() == RenewalPass()  # nothing is approved, so nothing is covered or renewed
    return p


async def test_a_purchase_two_months_old_approved_late_is_renewed_although_no_primary_renewal_exists(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    p = await _two_month_old_purchase(s)
    s.clock.today = date(2026, 12, 2)
    assert await s.extras() == CapacityExtras(0, 0)

    response = await s.decide(p["id"], "approve")

    assert response.status_code == 200, response.text
    (extra,) = await renewals(s, DEC)  # the supplementary renewal only: no primary renewal exists
    assert str(extra["source_order_id"]) == p["id"]
    assert (extra["kind"], extra["status"], extra["billing_month"]) == ("renewal", "awaiting_payment", DEC)
    assert (extra["extra_accounts"], extra["extra_agents"]) == (2, 1)
    assert (extra["days_charged"], extra["days_in_month"]) == (31, 31)
    assert (extra["amount_usd_cents"], extra["amount_vnd"]) == (4_500, 1_125_000)
    assert extra["grace_until"] == date(2026, 12, 8)  # seven days counting the 2nd
    # The extras take effect now and stay through the grace; the company is not asked twice.
    assert await s.extras(date(2026, 12, 2)) == CapacityExtras(2, 1)
    assert await s.extras(date(2026, 12, 8)) == CapacityExtras(2, 1)
    assert await s.extras(date(2026, 12, 9)) == CapacityExtras(0, 0)
    for who in ("oa", "oa2"):
        (note,) = await s.notifications(who, "capacity_renewal_due")
        assert str(note["ref_id"]) == str(extra["id"])
    assert await run_renewal_pass() == RenewalPass()  # October is outside November's coverage: no primary follows
    assert len(await renewals(s, DEC)) == 1
    assert (await s.decide(p["id"], "approve")).status_code == 200
    assert len(await renewals(s, DEC)) == 1


async def test_the_supplementary_renewal_of_a_two_month_old_purchase_rolls_into_the_next_primary_renewal(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    p = await _two_month_old_purchase(s)
    s.clock.today = date(2026, 12, 2)
    assert (await s.decide(p["id"], "approve")).status_code == 200
    (extra,) = await renewals(s, DEC)
    assert (await s.pay(extra["id"])).status_code == 200
    assert (await s.decide(extra["id"])).status_code == 200  # billed for the current month: nothing more is renewed
    assert len(await renewals(s, DEC)) == 1
    assert await s.extras(date(2026, 12, 31)) == CapacityExtras(2, 1)

    s.clock.today = JAN
    result = await run_renewal_pass()

    assert result == RenewalPass(created=1)
    (primary,) = await renewals(s, JAN)
    assert primary["source_order_id"] is None
    assert (primary["extra_accounts"], primary["extra_agents"]) == (2, 1)
    assert await s.extras() == CapacityExtras(2, 1)


@pytest.mark.parametrize(("billed", "renewed"), [(NOV, False), (SEP, True)])
async def test_late_approval_without_a_primary_renewal_is_renewed_unless_last_months_coverage_carries_it(
    capacity_scenario: CapacityScenario, billed: date, renewed: bool
):
    s = capacity_scenario
    await s.configure()
    order = await s.insert_order(status="submitted", month=billed, accounts=2, agents=1)
    s.clock.today = date(2026, 12, 2)

    assert (await s.decide(order)).status_code == 200

    assert [str(row["source_order_id"]) for row in await renewals(s, DEC)] == ([str(order)] if renewed else [])
    assert await s.extras() == CapacityExtras(2, 1)  # the supplementary renewal, or the previous month's fallback
    result = await run_renewal_pass()
    # Only last month's order feeds the primary renewal.
    assert result == RenewalPass(created=0 if renewed else 1)
    assert await s.extras() == CapacityExtras(2, 1)


async def test_an_older_order_approved_late_is_not_counted_twice_beside_last_months_coverage(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    await s.configure()
    s.clock.today = date(2026, 10, 30)
    old = await s.purchase(accounts=2, agents=1, approve=False)
    s.clock.today = date(2026, 11, 14)
    _ = await s.purchase(accounts=1)
    s.clock.today = date(2026, 12, 2)
    assert await s.extras() == CapacityExtras(1, 0)  # the fallback: November's coverage

    assert (await s.decide(old["id"])).status_code == 200

    (extra,) = await renewals(s, DEC)
    assert str(extra["source_order_id"]) == old["id"]
    assert await s.extras() == CapacityExtras(3, 1)  # the fallback plus the supplementary renewal
    assert await run_renewal_pass() == RenewalPass(created=1)
    primary, supplement = sorted(await renewals(s, DEC), key=lambda row: row["source_order_id"] is not None)
    assert primary["source_order_id"] is None
    assert (primary["extra_accounts"], primary["extra_agents"]) == (1, 0)  # November's coverage only
    assert (supplement["extra_accounts"], supplement["extra_agents"]) == (2, 1)
    assert await s.extras() == CapacityExtras(3, 1)


async def test_any_earlier_order_approved_late_is_renewed_including_a_renewal(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    await s.configure()
    november = await s.insert_order(
        kind="renewal", status="submitted", month=NOV, accounts=2, agents=0, grace_until=date(2026, 11, 7)
    )
    _ = await s.insert_order(
        kind="renewal", status="awaiting_payment", month=DEC, accounts=9, agents=0, grace_until=date(2026, 12, 7)
    )
    s.clock.today = date(2026, 12, 2)

    assert (await s.decide(november)).status_code == 200

    extras = [row for row in await renewals(s, DEC) if row["source_order_id"] is not None]
    assert [(str(row["source_order_id"]), row["extra_accounts"]) for row in extras] == [(str(november), 2)]


async def test_late_approval_needs_a_tier_and_survives_missing_payment_settings(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    _, p = await _late_setup(s)
    await s.db.execute("DELETE FROM system_settings WHERE key = 'capacity_payment_settings'")
    s.clock.today = date(2026, 12, 3)

    assert (await s.decide(p["id"])).status_code == 200

    (extra,) = [row for row in await renewals(s, DEC) if row["source_order_id"] is not None]
    # Without settings the renewal reuses the approved order's own rate and bank snapshot.
    assert extra["usd_vnd_rate"] == 25_000
    assert extra["recipient"] == {
        "bank_name": "Vietcombank",
        "account_number": "0011 0022 3344",
        "branch_name": "Sai Gon",
    }


async def test_a_company_that_lost_its_tier_gets_no_supplementary_renewal(capacity_scenario: CapacityScenario):
    s = capacity_scenario
    _, p = await _late_setup(s)
    await s.db.execute(
        "DELETE FROM organization_admission_requests WHERE provisioned_tenant_id = %(t)s", {"t": s.tenant_id}
    )
    s.clock.today = date(2026, 12, 3)

    response = await s.decide(p["id"])

    assert response.status_code == 200
    assert [row for row in await renewals(s, DEC) if row["source_order_id"] is not None] == []


# ── schema invariants ───────────────────────────────────────────────────


async def test_the_database_allows_one_primary_renewal_one_renewal_per_source_and_one_open_purchase(
    capacity_scenario: CapacityScenario,
):
    s = capacity_scenario
    primary = await s.insert_order(kind="renewal", status="awaiting_payment", month=DEC, grace_until=DEC)
    with pytest.raises(UniqueViolationError):
        _ = await s.insert_order(kind="renewal", status="awaiting_payment", month=DEC, grace_until=DEC)
    # Another month, another company and a supplementary renewal are all fine.
    _ = await s.insert_order(kind="renewal", status="awaiting_payment", month=JAN, grace_until=JAN)
    _ = await s.insert_order(kind="renewal", status="awaiting_payment", month=DEC, grace_until=DEC, tenant="other")
    source = await s.insert_order(status="approved", month=NOV)
    _ = await s.insert_order(
        kind="renewal", status="awaiting_payment", month=DEC, grace_until=DEC, source_order_id=source
    )
    with pytest.raises(UniqueViolationError):
        _ = await s.insert_order(
            kind="renewal", status="awaiting_payment", month=JAN, grace_until=JAN, source_order_id=source
        )
    _ = await s.insert_order(status="awaiting_payment", month=NOV)
    with pytest.raises(UniqueViolationError):
        _ = await s.insert_order(status="awaiting_payment", month=NOV)
    _ = await s.insert_order(status="awaiting_payment", month=NOV, tenant="other")
    with pytest.raises(UniqueViolationError):
        await s.db.execute(
            "UPDATE capacity_orders SET reference = (SELECT reference FROM capacity_orders WHERE id = %(id)s) "
            "WHERE id <> %(id)s AND kind = 'purchase' AND tenant_id = %(t)s",
            {"id": primary, "t": s.tenants["other"]},
        )


@pytest.mark.parametrize(
    "violation",
    [
        "status = 'submitted'",
        "status = 'approved'",
        (
            "status = 'rejected', evidence_storage_key = 'k', evidence_content_type = 'image/png', "
            "evidence_filename = 'f.png', evidence_size_bytes = 1, evidence_sha256 = repeat('a', 64), "
            "submitted_at = now(), reviewed_at = now()"
        ),
        "grace_until = '2026-12-07'",
        "extra_accounts = 0, extra_agents = 0",
        "extra_accounts = -1",
        "billing_month = '2026-11-02'",
        "days_charged = 31, days_in_month = 30",
        "days_charged = 0",
        "usd_vnd_rate = 0",
        "amount_vnd = -1",
        "tier = 'free'",
        "reference = 'MC123'",
        "kind = 'subscription'",
        "recipient = '[]'::jsonb",
        "status = 'cancelled', evidence_storage_key = 'k'",
        "status = 'expired', reviewed_at = now()",
    ],
)
async def test_the_database_refuses_inconsistent_orders(capacity_scenario: CapacityScenario, violation: str):
    s = capacity_scenario
    order = await s.insert_order(status="awaiting_payment", month=NOV, accounts=1)

    with pytest.raises(CheckViolationError):
        await s.db.execute(f"UPDATE capacity_orders SET {violation} WHERE id = %(id)s", {"id": order})  # noqa: S608


@pytest.mark.parametrize(
    "violation",
    [
        "grace_until = NULL",
        "days_charged = 5",
        "status = 'cancelled'",
    ],
)
async def test_the_database_refuses_inconsistent_renewals(capacity_scenario: CapacityScenario, violation: str):
    s = capacity_scenario
    order = await s.insert_order(kind="renewal", status="awaiting_payment", month=DEC, grace_until=DEC)

    with pytest.raises(CheckViolationError):
        await s.db.execute(f"UPDATE capacity_orders SET {violation} WHERE id = %(id)s", {"id": order})  # noqa: S608
