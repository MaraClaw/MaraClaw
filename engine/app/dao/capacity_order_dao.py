from datetime import date, datetime
from typing import ClassVar, Final, final
from uuid import UUID, uuid4

from app.core.json_types import uuid_from_row
from app.dao.base import BaseDAO
from app.db.types import as_jsonb
from app.records.capacity_order import (
    CapacityEvidence,
    CapacityOrderRecord,
    CapacityOrderStatus,
    NewCapacityOrder,
    new_reference,
)

REFERENCE_ATTEMPTS: Final = 5

_OPEN_STATUSES: Final = (CapacityOrderStatus.AWAITING_PAYMENT.value, CapacityOrderStatus.SUBMITTED.value)
_PRIMARY_RENEWAL: Final = "kind = 'renewal' AND source_order_id IS NULL"


@final
class CapacityOrderDAO(BaseDAO[CapacityOrderRecord]):
    """Purchase and renewal orders. Every status transition is one UPDATE guarded by the status it expects."""

    table: ClassVar[str] = "capacity_orders"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "tenant_id",
        "reference",
        "kind",
        "status",
        "source_order_id",
        "tier",
        "billing_month",
        "extra_accounts",
        "extra_agents",
        "account_unit_usd",
        "agent_unit_usd",
        "days_charged",
        "days_in_month",
        "amount_usd_cents",
        "usd_vnd_rate",
        "amount_vnd",
        "recipient",
        "grace_until",
        "evidence_storage_key",
        "evidence_content_type",
        "evidence_filename",
        "evidence_size_bytes",
        "evidence_sha256",
        "created_by",
        "created_at",
        "submitted_by",
        "submitted_at",
        "reviewed_by",
        "reviewed_at",
        "review_note",
    )
    record_factory = staticmethod(CapacityOrderRecord.from_row)

    async def insert(self, order: NewCapacityOrder) -> CapacityOrderRecord:
        """Insert an ``awaiting_payment`` order under a fresh reference (a collision draws another)."""
        params: dict[str, object] = {
            "id": uuid4(),
            "tenant_id": order.tenant_id,
            "kind": order.kind.value,
            "tier": order.tier.value,
            "billing_month": order.billing_month,
            "extra_accounts": order.extra_accounts,
            "extra_agents": order.extra_agents,
            "account_unit_usd": order.account_unit_usd,
            "agent_unit_usd": order.agent_unit_usd,
            "days_charged": order.days_charged,
            "days_in_month": order.days_in_month,
            "amount_usd_cents": order.amount_usd_cents,
            "usd_vnd_rate": order.usd_vnd_rate,
            "amount_vnd": order.amount_vnd,
            "recipient": as_jsonb(order.recipient.as_json()),
            "grace_until": order.grace_until,
            "source_order_id": order.source_order_id,
            "created_by": order.created_by,
        }
        async with self.session() as db:
            for _ in range(REFERENCE_ATTEMPTS):
                row = await db.fetchone(
                    f"INSERT INTO {self.table} (id, tenant_id, reference, kind, tier, billing_month, extra_accounts, "
                    "extra_agents, account_unit_usd, agent_unit_usd, days_charged, days_in_month, amount_usd_cents, "
                    "usd_vnd_rate, amount_vnd, recipient, grace_until, source_order_id, created_by) "
                    "VALUES (%(id)s, %(tenant_id)s, %(reference)s, %(kind)s, %(tier)s, %(billing_month)s, "
                    "%(extra_accounts)s, %(extra_agents)s, %(account_unit_usd)s, %(agent_unit_usd)s, "
                    "%(days_charged)s, %(days_in_month)s, %(amount_usd_cents)s, %(usd_vnd_rate)s, %(amount_vnd)s, "
                    "%(recipient)s, %(grace_until)s, %(source_order_id)s, %(created_by)s) "
                    f"ON CONFLICT (reference) DO NOTHING RETURNING {self._select_list()}",
                    {**params, "reference": new_reference()},
                )
                if row is not None:
                    return self.record_factory(row)
        raise RuntimeError("Could not allocate a unique capacity order reference")

    async def get_locked(self, order_id: UUID) -> CapacityOrderRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM {self.table} WHERE id = %(id)s FOR UPDATE", {"id": order_id}
            )
            return self.record_factory(row) if row is not None else None

    async def list_for_tenant(self, tenant_id: UUID, *, limit: int, offset: int) -> list[CapacityOrderRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM {self.table} WHERE tenant_id = %(tenant_id)s "
                "ORDER BY created_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
                {"tenant_id": tenant_id, "limit": limit, "offset": offset},
            )
            return [self.record_factory(row) for row in rows]

    async def list_by_status(
        self, status: CapacityOrderStatus | None, *, limit: int, offset: int
    ) -> list[CapacityOrderRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM {self.table} "
                "WHERE (%(status)s::text IS NULL OR status = %(status)s) "
                "ORDER BY created_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
                {"status": status.value if status is not None else None, "limit": limit, "offset": offset},
            )
            return [self.record_factory(row) for row in rows]

    async def open_for_tenant(self, tenant_id: UUID) -> list[CapacityOrderRecord]:
        """Orders still in play: awaiting payment or awaiting review, newest first."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM {self.table} "
                "WHERE tenant_id = %(tenant_id)s AND status = ANY(%(statuses)s) ORDER BY created_at DESC, id DESC",
                {"tenant_id": tenant_id, "statuses": list(_OPEN_STATUSES)},
            )
            return [self.record_factory(row) for row in rows]

    async def open_purchase(self, tenant_id: UUID) -> CapacityOrderRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM {self.table} "
                "WHERE tenant_id = %(tenant_id)s AND kind = 'purchase' AND status = 'awaiting_payment'",
                {"tenant_id": tenant_id},
            )
            return self.record_factory(row) if row is not None else None

    async def primary_renewal(self, tenant_id: UUID, month: date) -> CapacityOrderRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM {self.table} "
                f"WHERE tenant_id = %(tenant_id)s AND billing_month = %(month)s AND {_PRIMARY_RENEWAL}",
                {"tenant_id": tenant_id, "month": month},
            )
            return self.record_factory(row) if row is not None else None

    async def mark_submitted(
        self, order_id: UUID, evidence: CapacityEvidence, *, user_id: UUID, now: datetime
    ) -> CapacityOrderRecord | None:
        """``awaiting_payment`` to ``submitted`` with the evidence attached; ``None`` when the order moved on."""
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE {self.table} SET status = 'submitted', evidence_storage_key = %(key)s, "
                "evidence_content_type = %(content_type)s, evidence_filename = %(filename)s, "
                "evidence_size_bytes = %(size)s, evidence_sha256 = %(sha256)s, submitted_by = %(user)s, "
                "submitted_at = %(now)s WHERE id = %(id)s AND status = 'awaiting_payment' "
                f"RETURNING {self._select_list()}",
                {
                    "id": order_id,
                    "key": evidence.storage_key,
                    "content_type": evidence.content_type,
                    "filename": evidence.filename,
                    "size": evidence.size_bytes,
                    "sha256": evidence.sha256,
                    "user": user_id,
                    "now": now,
                },
            )
            return self.record_factory(row) if row is not None else None

    async def mark_cancelled(self, order_id: UUID) -> CapacityOrderRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE {self.table} SET status = 'cancelled' "
                "WHERE id = %(id)s AND status = 'awaiting_payment' AND kind = 'purchase' "
                f"RETURNING {self._select_list()}",
                {"id": order_id},
            )
            return self.record_factory(row) if row is not None else None

    async def mark_decided(
        self, order_id: UUID, status: CapacityOrderStatus, *, reviewer_id: UUID, note: str | None, now: datetime
    ) -> CapacityOrderRecord | None:
        """``submitted`` to ``approved`` or ``rejected``; ``None`` when the order is no longer submitted."""
        if status not in (CapacityOrderStatus.APPROVED, CapacityOrderStatus.REJECTED):
            raise ValueError("A review decision is approved or rejected")
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE {self.table} SET status = %(status)s, reviewed_by = %(reviewer)s, reviewed_at = %(now)s, "
                "review_note = %(note)s WHERE id = %(id)s AND status = 'submitted' "
                f"RETURNING {self._select_list()}",
                {"id": order_id, "status": status.value, "reviewer": reviewer_id, "now": now, "note": note},
            )
            return self.record_factory(row) if row is not None else None

    async def expire_stale(self, current_month: date, *, limit: int) -> int:
        """Expire up to ``limit`` awaiting-payment orders billed for an earlier month; the count that expired."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"WITH stale AS (SELECT id FROM {self.table} "
                "WHERE status = 'awaiting_payment' AND billing_month < %(month)s "
                "ORDER BY id LIMIT %(limit)s FOR UPDATE SKIP LOCKED) "
                f"UPDATE {self.table} o SET status = 'expired' FROM stale "
                "WHERE o.id = stale.id AND o.status = 'awaiting_payment' RETURNING o.id",
                {"month": current_month, "limit": limit},
            )
            return len(rows)

    async def tenants_due_for_renewal(
        self, month: date, previous_month: date, *, after: UUID | None, limit: int
    ) -> list[UUID]:
        """Tenants with approved orders for ``previous_month`` and no primary renewal for ``month`` yet, by id."""
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DISTINCT o.tenant_id FROM capacity_orders o "
                "WHERE o.billing_month = %(previous)s AND o.status = 'approved' "
                "AND (%(after)s::uuid IS NULL OR o.tenant_id > %(after)s) "
                "AND NOT EXISTS (SELECT 1 FROM capacity_orders p WHERE p.tenant_id = o.tenant_id "
                "AND p.billing_month = %(month)s AND p.kind = 'renewal' AND p.source_order_id IS NULL) "
                "ORDER BY o.tenant_id LIMIT %(limit)s",
                {"previous": previous_month, "month": month, "after": after, "limit": limit},
            )
            return [uuid_from_row(row["tenant_id"]) for row in rows]


capacity_order_dao = CapacityOrderDAO()
