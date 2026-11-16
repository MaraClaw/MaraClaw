"""Media joins the receipt transaction; cleanup consults committed references."""

from uuid import UUID

from app.db.session import connection_ctx
from app.db.types import as_jsonb
from app.records.channel_event_media import ChannelEventMediaRecord
from app.records.channel_event_receipt import ChannelEventReceiptRecord
from app.records.channel_media import StoredFile


class ChannelEventMediaDAO:
    async def insert(self, receipt: ChannelEventReceiptRecord, message_id: UUID, files: tuple[StoredFile, ...]) -> None:
        async with connection_ctx() as db:
            for ordinal, file in enumerate(files):
                await db.execute(
                    "INSERT INTO channel_event_media "
                    "(config_id, provider, event_key, ordinal, gateway_message_id, stored_file) "
                    "VALUES (%(config)s, %(provider)s, %(event)s, %(ordinal)s, %(message)s, %(file)s)",
                    {
                        "config": receipt.config_id,
                        "provider": receipt.provider,
                        "event": receipt.event_key,
                        "ordinal": ordinal,
                        "message": message_id,
                        "file": as_jsonb(file.model_dump(mode="json")),
                    },
                )

    async def for_message(self, message_id: UUID, agent_id: UUID) -> tuple[StoredFile, ...]:
        async with connection_ctx() as db:
            rows = await db.fetchall(
                "SELECT m.* FROM channel_event_media m JOIN channel_event_receipts r "
                "USING(config_id, provider, event_key) "
                "WHERE m.gateway_message_id = %(message)s AND r.agent_id = %(agent)s ORDER BY m.ordinal",
                {"message": message_id, "agent": agent_id},
            )
            return tuple(ChannelEventMediaRecord.from_row(row).file for row in rows)

    async def references(self, storage_key: str) -> bool:
        async with connection_ctx() as db:
            return bool(
                await db.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM channel_event_media WHERE stored_file->>'storage_key' = %(key)s)",
                    {"key": storage_key},
                )
            )

    async def is_zalo_turn(self, message_id: UUID, agent_id: UUID) -> bool:
        async with connection_ctx() as db:
            return bool(
                await db.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM gateway_report_deliveries d JOIN chat_sessions s "
                    "ON s.id = d.destination_id WHERE d.message_id = %(message)s "
                    "AND d.destination_kind = 'im' AND s.agent_id = %(agent)s "
                    "AND s.source_channel = 'zalo' AND s.external_conv_id LIKE 'zalo_personal:1:%%')",
                    {"message": message_id, "agent": agent_id},
                )
            )


channel_event_media_dao = ChannelEventMediaDAO()
