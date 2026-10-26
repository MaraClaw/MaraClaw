"""Transactional native-event reservations. Conflict never aborts the transaction."""

import json
from datetime import datetime

from app.core.json_types import datetime_from_row
from app.db.session import connection_ctx
from app.records.channel_event_receipt import ChannelEventReceiptRecord, ChannelReplyScope


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
                "INSERT INTO channel_event_receipts (config_id, provider, event_key, agent_id, tenant_id, fingerprint, "
                "provider_timestamp, provider_account_id, conversation_id) "
                "VALUES (%(config)s, %(provider)s, %(key)s, %(agent)s, %(tenant)s, %(fingerprint)s, "
                "%(timestamp)s, %(account)s, %(conversation)s) "
                "ON CONFLICT (config_id, provider, event_key) DO NOTHING RETURNING config_id",
                {
                    "config": receipt.config_id,
                    "provider": receipt.provider,
                    "key": receipt.event_key,
                    "agent": receipt.agent_id,
                    "tenant": receipt.tenant_id,
                    "fingerprint": receipt.fingerprint,
                    "timestamp": receipt.provider_timestamp,
                    "account": receipt.provider_account_id,
                    "conversation": receipt.conversation_id,
                },
            )
            return row is not None

    async def find(self, receipt: ChannelEventReceiptRecord) -> ChannelEventReceiptRecord | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "SELECT config_id, provider, event_key, agent_id, tenant_id, fingerprint, created_at, "
                "provider_timestamp, provider_account_id, conversation_id "
                "FROM channel_event_receipts WHERE config_id = %(config)s AND provider = %(provider)s "
                "AND event_key = %(key)s",
                {"config": receipt.config_id, "provider": receipt.provider, "key": receipt.event_key},
            )
            return ChannelEventReceiptRecord.from_row(row) if row is not None else None

    async def latest_eligible_timestamp(self, scope: ChannelReplyScope) -> datetime | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "SELECT r.provider_timestamp FROM channel_event_receipts r "
                "JOIN channel_configs c ON c.id = r.config_id AND c.agent_id = r.agent_id "
                "JOIN agents a ON a.id = r.agent_id AND a.tenant_id = r.tenant_id "
                "WHERE r.config_id = %(config)s AND r.agent_id = %(agent)s AND r.tenant_id = %(tenant)s "
                "AND r.provider = %(provider)s AND r.provider_account_id = %(account)s "
                "AND r.conversation_id = %(conversation)s AND r.provider_timestamp IS NOT NULL "
                "AND r.provider_timestamp <= r.created_at "
                "AND c.is_configured AND c.channel_type::text = r.provider AND c.app_id = r.provider_account_id "
                "ORDER BY r.provider_timestamp DESC LIMIT 1",
                {
                    "config": scope.config_id,
                    "agent": scope.agent_id,
                    "tenant": scope.tenant_id,
                    "provider": scope.provider,
                    "account": scope.account_id,
                    "conversation": scope.conversation_id,
                },
            )
            return datetime_from_row(row["provider_timestamp"]) if row is not None else None


channel_event_receipt_dao = ChannelEventReceiptDAO()
