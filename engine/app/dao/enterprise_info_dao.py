"""DAO for enterprise_info (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, final
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.enterprise_info import EnterpriseInfoRecord

_COLUMNS = (
    "id",
    "info_type",
    "content",
    "version",
    "visible_roles",
    "updated_by",
    "created_at",
    "updated_at",
)


@final
class EnterpriseInfoDAO(BaseDAO[EnterpriseInfoRecord]):
    table: ClassVar[str] = "enterprise_info"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(EnterpriseInfoRecord.from_row)

    async def get_by_type(self, info_type: str) -> EnterpriseInfoRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM enterprise_info WHERE info_type = %(info_type)s",
                {"info_type": info_type},
            )
            return EnterpriseInfoRecord.from_row(row) if row else None

    async def list_all(self) -> Sequence[EnterpriseInfoRecord]:
        async with self.session() as db:
            rows = await db.fetchall(f"SELECT {self._select_list()} FROM enterprise_info")
            return [EnterpriseInfoRecord.from_row(row) for row in rows]

    async def upsert(
        self,
        *,
        info_type: str,
        content: dict[str, Any],
        visible_roles: list[str],
        updated_by: UUID | None,
    ) -> EnterpriseInfoRecord:
        existing = await self.get_by_type(info_type)
        if existing:
            return await self.update(
                db_obj=existing,
                obj_in={
                    "content": content,
                    "visible_roles": visible_roles,
                    "version": (existing.version or 0) + 1,
                    "updated_by": updated_by,
                },
            )
        return await self.create(
            obj_in={
                "info_type": info_type,
                "content": content,
                "visible_roles": visible_roles,
                "version": 1,
                "updated_by": updated_by,
            }
        )


enterprise_info_dao = EnterpriseInfoDAO()
