"""The ledger that turns capacity orders into a company's effective extra accounts and agents.

``effective_extras(d)``, with ``M`` the Vietnam calendar month of ``d``, sums the tenant's orders billed for ``M``:

- every approved purchase;
- every renewal that is approved or submitted, and every awaiting-payment renewal while ``d <= grace_until``;

plus, while no primary renewal for ``M`` exists yet (the worker has not run, or payments are not configured), the
approved coverage of ``M - 1``: a company is never penalized before it was asked to pay. Rejected, expired and
cancelled orders count nothing. ``coverage(M)`` is the approved orders billed for ``M``, the quantities the next
month's primary renewal is generated from.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.json_types import int_from_row
from app.db.connection import DbConnection
from app.services.capacity_pricing import GRACE_DAYS, month_start, previous_month_start

_VIETNAM: Final = ZoneInfo("Asia/Ho_Chi_Minh")

EFFECTIVE_EXTRAS_SQL: Final = """
SELECT COALESCE(SUM(o.extra_accounts), 0) AS accounts, COALESCE(SUM(o.extra_agents), 0) AS agents
FROM capacity_orders o
WHERE o.tenant_id = %(tenant_id)s AND (
    (o.billing_month = %(month)s AND (
        (o.kind = 'purchase' AND o.status = 'approved')
        OR (o.kind = 'renewal' AND (
            o.status IN ('approved', 'submitted')
            OR (o.status = 'awaiting_payment' AND o.grace_until >= %(on_date)s)))))
    OR (o.billing_month = %(previous)s AND o.status = 'approved' AND NOT EXISTS (
        SELECT 1 FROM capacity_orders p
        WHERE p.tenant_id = o.tenant_id AND p.billing_month = %(month)s
            AND p.kind = 'renewal' AND p.source_order_id IS NULL)))
"""
COVERAGE_SQL: Final = (
    "SELECT COALESCE(SUM(extra_accounts), 0) AS accounts, COALESCE(SUM(extra_agents), 0) AS agents "
    "FROM capacity_orders WHERE tenant_id = %(tenant_id)s AND billing_month = %(month)s AND status = 'approved'"
)
PRIMARY_RENEWAL_EXISTS_SQL: Final = (
    "SELECT 1 FROM capacity_orders WHERE tenant_id = %(tenant_id)s AND billing_month = %(month)s "
    "AND kind = 'renewal' AND source_order_id IS NULL"
)


@dataclass(frozen=True, slots=True)
class CapacityExtras:
    accounts: int
    agents: int


def vn_today() -> date:
    return datetime.now(_VIETNAM).date()


def _last_grace_day(start: date) -> date:
    """The last of ``GRACE_DAYS`` grace days; the start day itself counts as the first."""
    return start + timedelta(days=GRACE_DAYS - 1)


def primary_grace_until(billing_month: date, created_on: date) -> date:
    """Last day an unpaid primary renewal still counts: the 7th of its month, or a week from its creation if later."""
    return max(_last_grace_day(month_start(billing_month)), _last_grace_day(created_on))


def supplementary_grace_until(created_on: date) -> date:
    return _last_grace_day(created_on)


def _extras(row: dict[str, object] | None) -> CapacityExtras:
    if row is None:
        return CapacityExtras(0, 0)
    return CapacityExtras(int_from_row(row["accounts"]), int_from_row(row["agents"]))


async def effective_extras(conn: DbConnection, tenant_id: UUID, on_date: date) -> CapacityExtras:
    month = month_start(on_date)
    row = await conn.fetchone(
        EFFECTIVE_EXTRAS_SQL,
        {"tenant_id": tenant_id, "month": month, "previous": previous_month_start(month), "on_date": on_date},
    )
    return _extras(row)


async def coverage(conn: DbConnection, tenant_id: UUID, month: date) -> CapacityExtras:
    """Approved extras billed for ``month``: what the next month's primary renewal covers."""
    return _extras(await conn.fetchone(COVERAGE_SQL, {"tenant_id": tenant_id, "month": month_start(month)}))


async def has_primary_renewal(conn: DbConnection, tenant_id: UUID, month: date) -> bool:
    row = await conn.fetchone(PRIMARY_RENEWAL_EXISTS_SQL, {"tenant_id": tenant_id, "month": month_start(month)})
    return row is not None
