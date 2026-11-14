"""The ledger's SQL, parameters and date rules, without a database (real semantics: test_capacity_orders_postgres)."""

import re
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from app.db.connection import DbConnection
from app.records.capacity_order import REFERENCE_LENGTH, CapacityOrderKind, CapacityOrderRecord, new_reference
from app.records.organization_plan import OrganizationPlanId
from app.services import capacity_ledger
from app.services.capacity_ledger import (
    COVERAGE_SQL,
    EFFECTIVE_EXTRAS_SQL,
    CapacityExtras,
    coverage,
    effective_extras,
    has_primary_renewal,
    primary_grace_until,
    supplementary_grace_until,
    vn_today,
)

TENANT = UUID(int=7)


class FakeConn:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def fetchone(self, query: str, params: dict[str, object]) -> dict[str, object] | None:
        self.calls.append((query, params))
        return self.row

    @property
    def db(self) -> DbConnection:
        return cast("DbConnection", self)


async def test_effective_extras_reads_the_month_of_the_date_and_the_previous_one():
    conn = FakeConn({"accounts": 3, "agents": 2})

    extras = await effective_extras(conn.db, TENANT, date(2027, 1, 15))

    assert extras == CapacityExtras(3, 2)
    ((sql, params),) = conn.calls
    assert sql == EFFECTIVE_EXTRAS_SQL
    assert params == {
        "tenant_id": TENANT,
        "month": date(2027, 1, 1),
        "previous": date(2026, 12, 1),
        "on_date": date(2027, 1, 15),
    }


async def test_a_tenant_without_orders_has_no_extras():
    conn = FakeConn(None)
    assert await effective_extras(conn.db, TENANT, date(2027, 1, 1)) == CapacityExtras(0, 0)


def test_the_effective_sql_encodes_the_final_counting_rules():
    sql = " ".join(EFFECTIVE_EXTRAS_SQL.split())
    # Purchases count only once approved.
    assert "o.kind = 'purchase' AND o.status = 'approved'" in sql
    # A renewal counts when approved or submitted, or while awaiting payment inside its grace.
    assert "o.kind = 'renewal' AND ( o.status IN ('approved', 'submitted')" in sql
    assert "o.status = 'awaiting_payment' AND o.grace_until >= %(on_date)s" in sql
    # Nothing else counts: rejected, expired and cancelled orders are never named.
    for ignored in ("'rejected'", "'expired'", "'cancelled'"):
        assert ignored not in sql
    # The previous month's approved coverage stands in only until a PRIMARY renewal for the month exists.
    assert "o.billing_month = %(previous)s AND o.status = 'approved' AND NOT EXISTS" in sql
    assert "p.kind = 'renewal' AND p.source_order_id IS NULL" in sql
    assert "p.billing_month = %(month)s" in sql
    # Every placeholder is a named parameter the function supplies.
    assert set(re.findall(r"%\((\w+)\)s", sql)) == {"tenant_id", "month", "previous", "on_date"}


async def test_coverage_counts_approved_orders_of_the_month_only():
    conn = FakeConn({"accounts": 4, "agents": 0})

    assert await coverage(conn.db, TENANT, date(2026, 12, 20)) == CapacityExtras(4, 0)

    ((sql, params),) = conn.calls
    assert sql == COVERAGE_SQL
    assert "status = 'approved'" in sql
    assert params == {"tenant_id": TENANT, "month": date(2026, 12, 1)}


@pytest.mark.parametrize(("row", "expected"), [({"one": 1}, True), (None, False)])
async def test_a_primary_renewal_is_a_renewal_without_a_source(row: dict[str, object] | None, expected: bool):
    conn = FakeConn(row)

    assert await has_primary_renewal(conn.db, TENANT, date(2027, 3, 9)) is expected

    ((sql, params),) = conn.calls
    assert "kind = 'renewal' AND source_order_id IS NULL" in sql
    assert params == {"tenant_id": TENANT, "month": date(2027, 3, 1)}


@pytest.mark.parametrize(
    ("month", "created", "expected"),
    [
        (date(2026, 12, 1), date(2026, 12, 1), date(2026, 12, 7)),
        (date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 8)),
        (date(2026, 12, 1), date(2026, 12, 20), date(2026, 12, 26)),
        (date(2026, 12, 1), date(2026, 12, 29), date(2027, 1, 4)),
    ],
)
def test_a_primary_renewal_has_seven_days_counting_the_first_or_its_creation_day_whichever_is_later(
    month: date, created: date, expected: date
):
    assert primary_grace_until(month, created) == expected


def test_a_supplementary_renewal_has_seven_days_counting_its_creation_day():
    assert supplementary_grace_until(date(2026, 12, 20)) == date(2026, 12, 26)


def test_the_vietnam_day_starts_seven_hours_ahead_of_utc(monkeypatch: pytest.MonkeyPatch):
    instant = datetime(2026, 12, 31, 18, 30, tzinfo=UTC)
    monkeypatch.setattr(capacity_ledger, "datetime", SimpleNamespace(now=lambda tz: instant.astimezone(tz)))

    assert vn_today() == date(2027, 1, 1)


def test_references_are_short_unique_looking_transfer_codes():
    references = {new_reference() for _ in range(200)}
    assert len(references) == 200
    for reference in references:
        assert re.fullmatch(r"MC[A-Z2-7]{8}", reference)
        assert len(reference) == 2 + REFERENCE_LENGTH


def test_a_row_decodes_into_an_order_with_its_snapshot_and_optional_evidence():
    row: dict[str, object] = {
        "id": UUID(int=1),
        "tenant_id": TENANT,
        "reference": "MCABCDEFGH",
        "kind": "renewal",
        "status": "submitted",
        "source_order_id": UUID(int=2),
        "tier": "max",
        "billing_month": date(2027, 1, 1),
        "extra_accounts": 2,
        "extra_agents": 1,
        "account_unit_usd": 4,
        "agent_unit_usd": 49,
        "days_charged": 31,
        "days_in_month": 31,
        "amount_usd_cents": 5700,
        "usd_vnd_rate": 25000,
        "amount_vnd": 1_425_000,
        "recipient": {"bank_name": "ACB", "account_number": "123", "branch_name": "HCM"},
        "grace_until": date(2027, 1, 7),
        "evidence_storage_key": "_capacity_payments/t/o/x.pdf",
        "evidence_content_type": "application/pdf",
        "evidence_filename": "proof.pdf",
        "evidence_size_bytes": 12,
        "evidence_sha256": "ab" * 32,
        "created_by": None,
        "created_at": datetime(2026, 12, 31, tzinfo=UTC),
        "submitted_by": UUID(int=3),
        "submitted_at": datetime(2027, 1, 2, tzinfo=UTC),
        "reviewed_by": None,
        "reviewed_at": None,
        "review_note": None,
    }

    order = CapacityOrderRecord.from_row(row)

    assert order.kind is CapacityOrderKind.RENEWAL
    assert order.tier is OrganizationPlanId.MAX
    assert order.recipient.account_number == "123"
    assert order.evidence is not None
    assert (order.evidence.filename, order.evidence.size_bytes) == ("proof.pdf", 12)
    assert order.grace_until == date(2027, 1, 7)

    bare = CapacityOrderRecord.from_row({**row, "evidence_storage_key": None})
    assert bare.evidence is None
