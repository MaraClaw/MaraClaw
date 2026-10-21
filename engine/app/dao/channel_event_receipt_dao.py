"""Transactional native-event reservations. Conflict never aborts the transaction."""

import json

from app.db.session import connection_ctx
from app.records.channel_event_receipt import ChannelEventReceiptRecord


class ChannelEventReceiptDAO:
    async def lock_conversation(self, receipt: ChannelEventReceiptRecord, conversation_id: str) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 0))",
                {"key": json.dumps([str(receipt.config_id), receipt.provider, conversation_id])},
            )

    async def reserve(self, receipt: ChannelEventReceiptRecord) -> bool:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "INSERT INTO channel_event_receipts (config_id, provider, event_key, agent_id, tenant_id, fingerprint) "
                "VALUES (%(config)s, %(provider)s, %(key)s, %(agent)s, %(tenant)s, %(fingerprint)s) "
                "ON CONFLICT (config_id, provider, event_key) DO NOTHING RETURNING config_id",
                {
                    "config": receipt.config_id,
                    "provider": receipt.provider,
                    "key": receipt.event_key,
                    "agent": receipt.agent_id,
                    "tenant": receipt.tenant_id,
                    "fingerprint": receipt.fingerprint,
                },
            )
            return row is not None

    async def find(self, receipt: ChannelEventReceiptRecord) -> ChannelEventReceiptRecord | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "SELECT config_id, provider, event_key, agent_id, tenant_id, fingerprint, created_at "
                "FROM channel_event_receipts WHERE config_id = %(config)s AND provider = %(provider)s "
                "AND event_key = %(key)s",
                {"config": receipt.config_id, "provider": receipt.provider, "key": receipt.event_key},
            )
            return ChannelEventReceiptRecord.from_row(row) if row is not None else None


channel_event_receipt_dao = ChannelEventReceiptDAO()
