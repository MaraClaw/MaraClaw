"""Monthly renewal and expiry of capacity orders.

On the first pass of a Vietnam month the worker creates one primary renewal per company whose approved coverage of the
previous month is not empty: the same quantities at the company's current tier prices, a whole month, the current
USD/VND rate and bank recipient. Creation runs under the tenant row lock and is idempotent (a partial unique index
allows one primary renewal per company and month). An order of an earlier month approved late also gets a
supplementary renewal, so what the company paid for still rolls into the current month; last month's order waits for
the primary renewal to exist, because the previous month's coverage already includes it.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.core.acl_locks import lock_tenants
from app.core.logging import logger
from app.dao.capacity_order_dao import capacity_order_dao as orders
from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.records.capacity_order import (
    CapacityOrderKind,
    CapacityOrderRecord,
    CapacityRecipient,
    NewCapacityOrder,
)
from app.records.organization_plan import OrganizationPlanId
from app.services import capacity_ledger
from app.services.capacity_notifications import notify_renewal_due
from app.services.capacity_payment_settings import PaymentSettings, load_payment_settings
from app.services.capacity_pricing import month_start, previous_month_start, quote_renewal
from app.services.organization_plans import plan_for, tier_for_tenant

EXPIRY_BATCH: Final = 500
TENANT_PAGE: Final = 50


class RenewalOutcome(StrEnum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    NOTHING_COVERED = "nothing_covered"
    NOT_ELIGIBLE = "not_eligible"
    PAYMENTS_NOT_CONFIGURED = "payments_not_configured"


@dataclass(frozen=True, slots=True)
class RenewalPass:
    expired: int = 0
    created: int = 0
    unconfigured: int = 0
    failed: int = 0


async def eligible_tier(db: DbConnection, tenant_id: UUID) -> OrganizationPlanId | None:
    """The tier of a company that may hold paid extras: tiered and not a system organization (checked first)."""
    is_system = await db.fetchval("SELECT is_system FROM tenants WHERE id = %(id)s", {"id": tenant_id})
    if is_system is None or bool(is_system):
        return None
    return await tier_for_tenant(tenant_id)


async def insert_renewal(
    tenant_id: UUID,
    tier: OrganizationPlanId,
    month: date,
    *,
    extra_accounts: int,
    extra_agents: int,
    usd_vnd_rate: int,
    recipient: CapacityRecipient,
    grace_until: date,
    source_order_id: UUID | None,
) -> CapacityOrderRecord:
    """A whole-month renewal at the company's current tier prices; the caller holds the tenant lock."""
    plan = plan_for(tier)
    quote = quote_renewal(
        month,
        extra_accounts=extra_accounts,
        extra_agents=extra_agents,
        account_unit_usd=int(plan.additional_account_monthly_usd),
        agent_unit_usd=int(plan.additional_agent_monthly_usd),
        usd_vnd_rate=usd_vnd_rate,
    )
    return await orders.insert(
        NewCapacityOrder(
            tenant_id=tenant_id,
            kind=CapacityOrderKind.RENEWAL,
            tier=tier,
            billing_month=quote.billing_month,
            extra_accounts=quote.extra_accounts,
            extra_agents=quote.extra_agents,
            account_unit_usd=quote.account_unit_usd,
            agent_unit_usd=quote.agent_unit_usd,
            days_charged=quote.days_charged,
            days_in_month=quote.days_in_month,
            amount_usd_cents=quote.amount_usd_cents,
            usd_vnd_rate=quote.usd_vnd_rate,
            amount_vnd=quote.amount_vnd,
            recipient=recipient,
            grace_until=grace_until,
            source_order_id=source_order_id,
        )
    )


async def generate_primary_renewal(tenant_id: UUID, today: date) -> RenewalOutcome:
    """Create the company's primary renewal for ``today``'s month when one is due; safe to repeat."""
    month = month_start(today)
    async with connection_ctx() as db:
        await lock_tenants(db, (tenant_id,))
        if await capacity_ledger.has_primary_renewal(db, tenant_id, month):
            return RenewalOutcome.ALREADY_EXISTS
        covered = await capacity_ledger.coverage(db, tenant_id, previous_month_start(month))
        if covered.accounts + covered.agents == 0:
            return RenewalOutcome.NOTHING_COVERED
        tier = await eligible_tier(db, tenant_id)
        if tier is None:
            return RenewalOutcome.NOT_ELIGIBLE
        settings = await load_payment_settings()
        if settings is None:
            return RenewalOutcome.PAYMENTS_NOT_CONFIGURED
        order = await insert_renewal(
            tenant_id,
            tier,
            month,
            extra_accounts=covered.accounts,
            extra_agents=covered.agents,
            usd_vnd_rate=settings.usd_vnd_rate,
            recipient=settings.recipient,
            grace_until=capacity_ledger.primary_grace_until(month, today),
            source_order_id=None,
        )
        _ = await notify_renewal_due(order)
        return RenewalOutcome.CREATED


async def supplement_late_approval(
    db: DbConnection, approved: CapacityOrderRecord, today: date, settings: PaymentSettings | None
) -> CapacityOrderRecord | None:
    """Renew an order approved after its month, so what the company paid for still rolls into the current month.

    An order billed for the previous month is left alone while the current month has no primary renewal: the ledger
    fallback and generation both count it through the previous month's coverage. An older order is outside that
    coverage, so it is always renewed, primary renewal or not. A company that is no longer a tiered, non-system one
    needs no renewal. The caller holds the tenant lock. Without current payment settings the approved order's own
    rate and recipient are used.
    """
    month = month_start(today)
    if approved.billing_month >= month:
        return None
    covered_by_previous_month = approved.billing_month == previous_month_start(month)
    if covered_by_previous_month and not await capacity_ledger.has_primary_renewal(db, approved.tenant_id, month):
        return None
    tier = await eligible_tier(db, approved.tenant_id)
    if tier is None:
        return None
    rate = settings.usd_vnd_rate if settings is not None else approved.usd_vnd_rate
    recipient = settings.recipient if settings is not None else approved.recipient
    order = await insert_renewal(
        approved.tenant_id,
        tier,
        month,
        extra_accounts=approved.extra_accounts,
        extra_agents=approved.extra_agents,
        usd_vnd_rate=rate,
        recipient=recipient,
        grace_until=capacity_ledger.supplementary_grace_until(today),
        source_order_id=approved.id,
    )
    _ = await notify_renewal_due(order)
    return order


async def expire_stale_orders(today: date) -> int:
    """Expire every awaiting-payment order billed for an earlier month, one short transaction per batch."""
    month = month_start(today)
    total = 0
    while True:
        async with connection_ctx():
            expired = await orders.expire_stale(month, limit=EXPIRY_BATCH)
        total += expired
        if expired < EXPIRY_BATCH:
            return total


async def run_renewal_pass(today: date | None = None) -> RenewalPass:
    """Expire stale orders, then create the due primary renewals; one company's failure never stops the pass."""
    today = today or capacity_ledger.vn_today()
    month = month_start(today)
    previous = previous_month_start(month)
    expired = await expire_stale_orders(today)
    created = unconfigured = failed = 0
    after: UUID | None = None
    while True:
        async with connection_ctx():
            due = await orders.tenants_due_for_renewal(month, previous, after=after, limit=TENANT_PAGE)
        for tenant_id in due:
            try:
                outcome = await generate_primary_renewal(tenant_id, today)
            except Exception as exc:
                failed += 1
                logger.bind(exception_type=type(exc).__name__).warning(
                    "[capacity] generating the renewal failed tenant={}", tenant_id
                )
                continue
            if outcome is RenewalOutcome.CREATED:
                created += 1
            elif outcome is RenewalOutcome.PAYMENTS_NOT_CONFIGURED:
                unconfigured += 1
        if len(due) < TENANT_PAGE:
            break
        after = due[-1]
    if unconfigured:
        logger.warning(
            "[capacity] {} renewal(s) are due but payment settings are not configured; retrying next pass", unconfigured
        )
    return RenewalPass(expired=expired, created=created, unconfigured=unconfigured, failed=failed)
