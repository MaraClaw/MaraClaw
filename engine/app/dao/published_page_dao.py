"""DAO for published_pages (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.published_page import PublishedPageRecord

_COLUMNS = (
    "id",
    "short_id",
    "agent_id",
    "user_id",
    "tenant_id",
    "source_path",
    "title",
    "view_count",
    "created_at",
)


class PublishedPageDAO(BaseDAO[PublishedPageRecord]):
    table = "published_pages"
    columns = _COLUMNS
    record_factory = staticmethod(PublishedPageRecord.from_row)

    async def get_by_short_id(self, short_id: str) -> PublishedPageRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM published_pages WHERE short_id = %(short_id)s",
                {"short_id": short_id},
            )
            return PublishedPageRecord.from_row(row) if row else None

    async def list_for_agent(self, agent_id: UUID) -> Sequence[PublishedPageRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM published_pages "
                "WHERE agent_id = %(agent_id)s ORDER BY created_at DESC",
                {"agent_id": agent_id},
            )
            return [PublishedPageRecord.from_row(row) for row in rows]

    async def increment_view_count(self, page_id: UUID) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE published_pages SET view_count = COALESCE(view_count, 0) + 1 WHERE id = %(id)s",
                {"id": page_id},
            )


published_page_dao = PublishedPageDAO()
