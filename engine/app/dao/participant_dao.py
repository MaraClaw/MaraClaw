"""DAO for participants table (psycopg)."""

from __future__ import annotations

from uuid import UUID

from typing import Any, ClassVar

from app.dao.base import BaseDAO
from app.records.participant import ParticipantRecord

_COLUMNS = ("id", "type", "ref_id", "display_name", "avatar_url", "created_at")


class ParticipantDAO(BaseDAO[ParticipantRecord]):
    """DAO for Participant records."""

    table: ClassVar[str] = "participants"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory: Any = staticmethod(ParticipantRecord.from_row)

    async def create_for_user(
        self,
        user_id: UUID,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> ParticipantRecord:
        return await self.create(
            obj_in={
                "type": "user",
                "ref_id": user_id,
                "display_name": display_name or "User",
                "avatar_url": avatar_url,
            }
        )

    async def get_by_type_ref(self, type: str, ref_id: UUID) -> ParticipantRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM participants WHERE type = %(type)s AND ref_id = %(ref_id)s LIMIT 1",
                {"type": type, "ref_id": ref_id},
            )
            return ParticipantRecord.from_row(row) if row else None


participant_dao = ParticipantDAO()
