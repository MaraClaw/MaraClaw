"""The capacity renewal loop: when it beats, how it backs off, how a pass reports and which late approvals are renewed
(no database)."""

import asyncio
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

import pytest
from psycopg import OperationalError

from app.config import get_settings
from app.db.connection import DbConnection
from app.db.errors import DbError
from app.records.capacity_order import (
    CapacityOrderKind,
    CapacityOrderRecord,
    CapacityOrderStatus,
    CapacityRecipient,
)
from app.records.organization_plan import OrganizationPlanId
from app.services import capacity_ledger, capacity_renewal_worker as worker, capacity_renewals
from app.services.capacity_renewals import RenewalPass
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]


class Passes:
    """Scripted ``run_renewal_pass`` outcomes: an exception is raised, a result is returned."""

    def __init__(self, *outcomes: RenewalPass | Exception) -> None:
        self.outcomes: list[RenewalPass | Exception] = list(outcomes)
        self.calls: int = 0

    async def __call__(self) -> RenewalPass:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else RenewalPass()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP) -> list[float]:
    recorded: list[float] = []

    async def paced(seconds: float) -> None:
        recorded.append(seconds)
        await worker_http.pacing.wait(seconds)

    monkeypatch.setattr(worker, "sleep", paced)
    return recorded


def start(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, passes: Passes) -> asyncio.Task[object]:
    monkeypatch.setattr(worker, "run_renewal_pass", passes)
    return worker_http.start_loop(
        "capacity_renewal", worker.start_capacity_renewal_worker(), budget_seconds=worker.progress_budget_seconds()
    )


def test_the_interval_defaults_to_an_hour_and_the_budget_adds_the_pass_allowance(monkeypatch: pytest.MonkeyPatch):
    assert get_settings().CAPACITY_RENEWAL_INTERVAL_SECONDS == 3600.0
    monkeypatch.setattr(get_settings(), "CAPACITY_RENEWAL_INTERVAL_SECONDS", 1800.0)

    assert worker.progress_budget_seconds() == 1800.0 + worker.PASS_ALLOWANCE_SECONDS


async def test_a_finished_pass_beats_and_the_loop_sleeps_the_interval(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    passes = Passes(RenewalPass(expired=2, created=1))
    task = start(monkeypatch, worker_http, passes)

    await worker_http.pacing.paused()

    assert passes.calls == 1
    assert sleeps == [get_settings().CAPACITY_RENEWAL_INTERVAL_SECONDS]
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)
    await worker_http.pacing.step()
    assert passes.calls == 2
    assert not task.done()


async def test_a_failed_company_inside_a_pass_does_not_stop_the_loop_from_beating(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # The pass itself counts and logs a company's failure; the loop only sees the finished result.
    task = start(monkeypatch, worker_http, Passes(RenewalPass(failed=1)))

    await worker_http.pacing.paused()

    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)
    assert not task.done()


async def test_database_errors_back_off_without_beating_then_recover(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    passes = Passes(DbError("down"), OperationalError("down"))
    task = start(monkeypatch, worker_http, passes)
    await worker_http.pacing.paused()
    worker_http.pacing.now += worker.progress_budget_seconds() + 1
    await worker_http.ready(expected=False)
    await worker_http.pacing.step()
    await worker_http.ready(expected=False)
    assert not task.done()

    await worker_http.pacing.step()

    await worker_http.ready(expected=True)
    assert sleeps == [
        worker.DB_RETRY_BASE_SECONDS,
        worker.DB_RETRY_BASE_SECONDS * 2,
        get_settings().CAPACITY_RENEWAL_INTERVAL_SECONDS,
    ]
    assert passes.calls == 3


async def test_a_pass_uses_the_vietnam_date_and_expires_before_it_renews(monkeypatch: pytest.MonkeyPatch):
    today = date(2027, 2, 1)
    calls: list[str] = []

    async def expire(day: date) -> int:
        assert day == today
        calls.append("expire")
        return 3

    class Due:
        async def tenants_due_for_renewal(
            self, month: date, previous: date, *, after: object, limit: int
        ) -> list[object]:
            assert (month, previous, after, limit) == (
                date(2027, 2, 1),
                date(2027, 1, 1),
                None,
                capacity_renewals.TENANT_PAGE,
            )
            calls.append("due")
            return []

    class Connection:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(capacity_renewals, "expire_stale_orders", expire)
    monkeypatch.setattr(capacity_renewals, "orders", Due())
    monkeypatch.setattr(capacity_renewals, "connection_ctx", Connection)

    result = await capacity_renewals.run_renewal_pass(today)

    assert calls == ["expire", "due"]
    assert result == RenewalPass(expired=3)


def approved_order(billing_month: date) -> CapacityOrderRecord:
    return CapacityOrderRecord(
        id=UUID(int=1),
        tenant_id=UUID(int=2),
        reference="MCABCDEFGH",
        kind=CapacityOrderKind.PURCHASE,
        status=CapacityOrderStatus.APPROVED,
        tier=OrganizationPlanId.PRO,
        billing_month=billing_month,
        extra_accounts=2,
        extra_agents=1,
        account_unit_usd=5,
        agent_unit_usd=35,
        days_charged=10,
        days_in_month=30,
        amount_usd_cents=1500,
        usd_vnd_rate=25_000,
        amount_vnd=375_000,
        recipient=CapacityRecipient(bank_name="ACB", account_number="123", branch_name="HCM"),
        created_at=datetime(2026, 10, 30, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("billed", "primary_exists", "renewed"),
    [
        (date(2026, 12, 1), True, False),
        (date(2026, 12, 1), False, False),
        (date(2026, 11, 1), True, True),
        (date(2026, 11, 1), False, False),
        (date(2026, 10, 1), True, True),
        (date(2026, 10, 1), False, True),
        (date(2026, 9, 1), False, True),
    ],
)
async def test_a_late_approval_is_renewed_unless_it_is_this_month_or_last_months_before_the_primary_exists(
    monkeypatch: pytest.MonkeyPatch, billed: date, primary_exists: bool, renewed: bool
):
    today = date(2026, 12, 2)
    order = approved_order(billed)
    inserted: list[dict[str, object]] = []

    async def has_primary(_db: DbConnection, tenant_id: UUID, month: date) -> bool:
        assert (tenant_id, month) == (order.tenant_id, date(2026, 12, 1))
        return primary_exists

    async def tier(_db: DbConnection, _tenant_id: UUID) -> OrganizationPlanId:
        return OrganizationPlanId.PRO

    async def insert(tenant_id: UUID, plan: OrganizationPlanId, month: date, **fields: object) -> CapacityOrderRecord:
        inserted.append({"tenant_id": tenant_id, "tier": plan, "month": month, **fields})
        return order

    async def notify(_order: CapacityOrderRecord) -> int:
        return 0

    monkeypatch.setattr(capacity_ledger, "has_primary_renewal", has_primary)
    monkeypatch.setattr(capacity_renewals, "eligible_tier", tier)
    monkeypatch.setattr(capacity_renewals, "insert_renewal", insert)
    monkeypatch.setattr(capacity_renewals, "notify_renewal_due", notify)

    result = await capacity_renewals.supplement_late_approval(cast("DbConnection", object()), order, today, None)

    assert (result is not None) is renewed
    expected = {
        "tenant_id": order.tenant_id,
        "tier": OrganizationPlanId.PRO,
        "month": date(2026, 12, 1),
        "extra_accounts": 2,
        "extra_agents": 1,
        "usd_vnd_rate": 25_000,
        "recipient": order.recipient,
        "grace_until": date(2026, 12, 8),
        "source_order_id": order.id,
    }
    assert inserted == ([expected] if renewed else [])
