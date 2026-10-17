"""Durable report intents and fenced delivery leases."""

from datetime import datetime, timedelta
from typing import ClassVar, final
from uuid import UUID, uuid4

from app.dao.base import BaseDAO
from app.records.gateway_report_delivery import (
    DestinationKind,
    GatewayReportDeliveryRecord,
    TerminalReason,
)

_COLUMNS = (
    "id",
    "message_id",
    "destination_kind",
    "destination_id",
    "tenant_id",
    "available_at",
    "attempts",
    "claim_token",
    "lease_expires_at",
    "delivered_at",
    "terminal_reason",
    "terminal_at",
)
_FENCE = """
    id = %(id)s AND claim_token = %(claim_token)s
    AND delivered_at IS NULL AND terminal_at IS NULL AND lease_expires_at > %(now)s
"""


@final
class GatewayReportDeliveryDAO(BaseDAO[GatewayReportDeliveryRecord]):
    table: ClassVar[str] = "gateway_report_deliveries"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(GatewayReportDeliveryRecord.from_row)

    async def create_intent(
        self,
        *,
        message_id: UUID,
        destination_kind: DestinationKind,
        destination_id: UUID,
        tenant_id: UUID | None,
    ) -> GatewayReportDeliveryRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"""INSERT INTO {self.table}
                    (id, message_id, destination_kind, destination_id, tenant_id)
                    VALUES (%(id)s, %(message_id)s, %(destination_kind)s,
                            %(destination_id)s, %(tenant_id)s)
                    ON CONFLICT (message_id, destination_kind, destination_id) DO NOTHING
                    RETURNING {self._select_list()}""",
                {
                    "id": uuid4(),
                    "message_id": message_id,
                    "destination_kind": destination_kind,
                    "destination_id": destination_id,
                    "tenant_id": tenant_id,
                },
            )
        return self.record_factory(row) if row is not None else None

    async def claim_batch(
        self,
        *,
        limit: int,
        lease_seconds: float,
        now: datetime,
    ) -> list[GatewayReportDeliveryRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"""WITH due AS (
                    SELECT id FROM {self.table}
                    WHERE delivered_at IS NULL AND terminal_at IS NULL AND available_at <= %(now)s
                      AND (lease_expires_at IS NULL OR lease_expires_at <= %(now)s)
                    ORDER BY available_at, id
                    LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                )
                UPDATE {self.table} AS delivery
                SET claim_token = gen_random_uuid(),
                    lease_expires_at = %(lease_expires_at)s,
                    attempts = delivery.attempts + 1
                FROM due WHERE delivery.id = due.id
                RETURNING {self._select_list("delivery")}""",
                {"limit": limit, "now": now, "lease_expires_at": now + timedelta(seconds=lease_seconds)},
            )
        return [self.record_factory(row) for row in rows]

    async def lock_claim(self, claim: GatewayReportDeliveryRecord, *, now: datetime) -> bool:
        """Lock a live claim; caller must retain an outer connection_ctx transaction."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT id FROM {self.table} WHERE {_FENCE} FOR UPDATE",
                {"id": claim.id, "claim_token": claim.claim_token, "now": now},
            )
        return row is not None

    async def acknowledge(self, claim: GatewayReportDeliveryRecord, *, now: datetime) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                f"""UPDATE {self.table} SET delivered_at = %(now)s,
                    claim_token = NULL, lease_expires_at = NULL
                    WHERE {_FENCE} RETURNING id""",
                {"id": claim.id, "claim_token": claim.claim_token, "now": now},
            )
        return row is not None

    async def finish_terminal(
        self,
        id: UUID,
        claim_token: UUID,
        reason: TerminalReason,
        *,
        now: datetime,
    ) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                f"""UPDATE {self.table} SET terminal_reason = %(reason)s, terminal_at = %(now)s,
                    claim_token = NULL, lease_expires_at = NULL
                    WHERE {_FENCE} RETURNING id""",
                {"id": id, "claim_token": claim_token, "reason": reason, "now": now},
            )
        return row is not None

    async def retry(
        self,
        claim: GatewayReportDeliveryRecord,
        *,
        available_at: datetime,
        now: datetime,
    ) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                f"""UPDATE {self.table} SET available_at = %(available_at)s,
                    claim_token = NULL, lease_expires_at = NULL
                    WHERE {_FENCE} RETURNING id""",
                {"id": claim.id, "claim_token": claim.claim_token, "available_at": available_at, "now": now},
            )
        return row is not None


gateway_report_delivery_dao = GatewayReportDeliveryDAO()
