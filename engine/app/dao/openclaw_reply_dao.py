from collections.abc import Sequence
from typing import ClassVar, final
from uuid import UUID, uuid5

from app.core.json_types import str_from_row, uuid_from_row
from app.dao.base import BaseDAO
from app.db.types import as_jsonb
from app.records.chat import ChatMessageRecord
from app.records.chat_attachment import chat_attachments_adapter
from app.records.openclaw_reply_binding import OpenClawReplyBinding
from app.services.openclaw_transcript import ReplyMedia, TranscriptReply

AGENT_PAGE = 20
RECEIPT_PAGE = 20


@final
class OpenClawReplyDAO(BaseDAO[OpenClawReplyBinding]):
    table: ClassVar[str] = "openclaw_reply_bindings"
    columns: ClassVar[tuple[str, ...]] = ("id", "agent_id", "session_id", "owner_id", "tenant_id", "user_id", "cursor")
    record_factory = staticmethod(OpenClawReplyBinding.from_row)

    async def model_refs(self, binding_id: UUID) -> list[str]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DISTINCT guest_model_ref FROM gateway_messages "
                "WHERE reply_binding_id = %(id)s AND guest_model_ref IS NOT NULL",
                {"id": binding_id},
            )
            return [str_from_row(row["guest_model_ref"]) for row in rows]

    async def pending_media_many(self, binding_ids: Sequence[UUID]) -> dict[UUID, tuple[TranscriptReply, ...]]:
        if not binding_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT binding_id, event_id, pending_text FROM ("
                "SELECT binding_id, event_id, pending_text, checked_at, row_number() OVER "
                "(PARTITION BY binding_id ORDER BY checked_at, event_id) AS receipt_rank FROM openclaw_reply_receipts "
                "WHERE binding_id = ANY(%(ids)s) AND pending_text IS NOT NULL) ranked "
                "WHERE receipt_rank <= %(limit)s ORDER BY binding_id, checked_at, event_id",
                {"ids": list(binding_ids), "limit": RECEIPT_PAGE},
            )
        grouped: dict[UUID, list[TranscriptReply]] = {}
        for row in rows:
            reply = TranscriptReply(str_from_row(row["event_id"]), "", str_from_row(row["pending_text"]))
            grouped.setdefault(uuid_from_row(row["binding_id"]), []).append(reply)
        return {binding_id: tuple(replies) for binding_id, replies in grouped.items()}

    async def pending_media(self, binding_id: UUID) -> tuple[TranscriptReply, ...]:
        return (await self.pending_media_many((binding_id,))).get(binding_id, ())

    async def page(self, after: UUID | None) -> tuple[OpenClawReplyBinding, ...]:
        """Every binding of the next ``AGENT_PAGE`` agents after ``after``, so an agent group is never split."""
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT b.* FROM openclaw_reply_bindings b WHERE b.agent_id IN ("
                "SELECT agent_id FROM openclaw_reply_bindings WHERE (%(after)s::uuid IS NULL OR agent_id > %(after)s) "
                "GROUP BY agent_id ORDER BY agent_id LIMIT %(limit)s) ORDER BY b.agent_id, b.id",
                {"after": after, "limit": AGENT_PAGE},
            )
            return tuple(OpenClawReplyBinding.from_row(row) for row in rows)

    async def lock(self, binding: OpenClawReplyBinding) -> OpenClawReplyBinding | None:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT * FROM openclaw_reply_bindings WHERE id = %(id)s FOR UPDATE", {"id": binding.id}
            )
            return OpenClawReplyBinding.from_row(row) if row is not None else None

    async def is_native_run(self, agent_id: UUID, run_id: str) -> bool:
        try:
            native_id = UUID(run_id)
        except ValueError:
            return False
        async with self.session() as db:
            return bool(
                await db.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM gateway_messages WHERE id = %(id)s AND agent_id = %(agent)s)",
                    {"id": native_id, "agent": agent_id},
                )
            )

    async def persist(
        self, binding: OpenClawReplyBinding, reply: TranscriptReply, media: ReplyMedia
    ) -> ChatMessageRecord | None:
        message_id = uuid5(binding.id, reply.id)
        async with self.session() as db:
            await db.execute(
                "INSERT INTO openclaw_reply_receipts (binding_id, event_id, message_id, pending_text) "
                "VALUES (%(binding)s, %(event)s, %(message)s, %(pending)s) ON CONFLICT (binding_id, event_id) "
                "DO UPDATE SET pending_text = EXCLUDED.pending_text, checked_at = clock_timestamp()",
                {
                    "binding": binding.id,
                    "event": reply.id,
                    "message": message_id,
                    "pending": reply.text if media.pending else None,
                },
            )
            if not media.text and not media.attachments:
                return None
            previous = await db.fetchone("SELECT attachments FROM chat_messages WHERE id = %(id)s", {"id": message_id})
            attachments = list(media.attachments)
            if previous is not None:
                attachments = list(
                    dict.fromkeys((*chat_attachments_adapter.validate_python(previous["attachments"]), *attachments))
                )[:10]
            row = await db.fetchone(
                "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, attachments) "
                "VALUES (%(id)s, %(agent)s, %(user)s, 'assistant', %(content)s, %(session)s, %(attachments)s) "
                "ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, attachments = EXCLUDED.attachments "
                "WHERE chat_messages.content IS DISTINCT FROM EXCLUDED.content "
                "OR chat_messages.attachments IS DISTINCT FROM EXCLUDED.attachments RETURNING *",
                {
                    "id": message_id,
                    "agent": binding.agent_id,
                    "user": binding.user_id,
                    "content": media.text,
                    "session": str(binding.session_id),
                    "attachments": as_jsonb([ref.as_dict() for ref in attachments]),
                },
            )
            if row is not None:
                await db.execute(
                    "UPDATE openclaw_reply_receipts SET publish_pending = TRUE WHERE binding_id = %(binding)s "
                    "AND event_id = %(event)s",
                    {"binding": binding.id, "event": reply.id},
                )
            return ChatMessageRecord.from_row(row) if row is not None else None

    async def pending_publications_many(self, binding_ids: Sequence[UUID]) -> dict[UUID, tuple[ChatMessageRecord, ...]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT * FROM (SELECT m.*, r.binding_id, row_number() OVER "
                "(PARTITION BY r.binding_id ORDER BY m.created_at, m.id) AS receipt_rank "
                "FROM chat_messages m JOIN openclaw_reply_receipts r ON r.message_id = m.id "
                "WHERE r.binding_id = ANY(%(ids)s) AND r.publish_pending) ranked "
                "WHERE receipt_rank <= %(limit)s ORDER BY binding_id, created_at, id",
                {"ids": list(binding_ids), "limit": RECEIPT_PAGE},
            )
        grouped: dict[UUID, list[ChatMessageRecord]] = {}
        for row in rows:
            grouped.setdefault(uuid_from_row(row["binding_id"]), []).append(ChatMessageRecord.from_row(row))
        return {binding_id: tuple(messages) for binding_id, messages in grouped.items()}

    async def pending_publications(self, binding_id: UUID) -> tuple[ChatMessageRecord, ...]:
        return (await self.pending_publications_many((binding_id,))).get(binding_id, ())

    async def checkpoint(self, binding_id: UUID, cursor: dict[str, int]) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE openclaw_reply_bindings SET cursor = %(cursor)s WHERE id = %(id)s",
                {"id": binding_id, "cursor": as_jsonb(cursor)},
            )


openclaw_reply_dao = OpenClawReplyDAO()
