"""DAO for invitation_codes table (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.dao.base import BaseDAO
from app.records.invitation import InvitationCodeRecord

_COLUMNS = (
    "id",
    "code",
    "tenant_id",
    "max_uses",
    "used_count",
    "is_active",
    "created_by",
    "created_at",
)


class InvitationCodeDAO(BaseDAO[InvitationCodeRecord]):
    """DAO for InvitationCode records."""

    table = "invitation_codes"
    columns = _COLUMNS
    record_factory = staticmethod(InvitationCodeRecord.from_row)

    async def get_active_by_code(self, code: str) -> InvitationCodeRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM invitation_codes "
                "WHERE code = %(code)s AND is_active IS TRUE AND tenant_id IS NOT NULL LIMIT 1",
                {"code": code},
            )
            return InvitationCodeRecord.from_row(row) if row else None

    async def get_for_tenant(self, code_id: Any, tenant_id: Any) -> InvitationCodeRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM invitation_codes "
                "WHERE id = %(id)s AND tenant_id = %(tenant_id)s LIMIT 1",
                {"id": code_id, "tenant_id": tenant_id},
            )
            return InvitationCodeRecord.from_row(row) if row else None

    async def count_for_tenant(self, tenant_id: Any, *, search: str | None = None) -> int:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        search_sql = ""
        if search:
            params["search"] = f"%{search}%"
            search_sql = " AND code ILIKE %(search)s"
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT COUNT(*) FROM invitation_codes WHERE tenant_id = %(tenant_id)s{search_sql}",
                params,
            )
            return int(value or 0)

    async def list_for_tenant(
        self,
        tenant_id: Any,
        *,
        search: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> Sequence[InvitationCodeRecord]:
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "offset": offset,
            "limit": limit,
        }
        search_sql = ""
        if search:
            params["search"] = f"%{search}%"
            search_sql = " AND code ILIKE %(search)s"
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM invitation_codes "
                f"WHERE tenant_id = %(tenant_id)s{search_sql} "
                "ORDER BY created_at DESC NULLS LAST OFFSET %(offset)s LIMIT %(limit)s",
                params,
            )
            return [InvitationCodeRecord.from_row(row) for row in rows]

    async def list_all_for_tenant(self, tenant_id: Any) -> Sequence[InvitationCodeRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM invitation_codes "
                "WHERE tenant_id = %(tenant_id)s ORDER BY created_at ASC NULLS LAST",
                {"tenant_id": tenant_id},
            )
            return [InvitationCodeRecord.from_row(row) for row in rows]


invitation_code_dao = InvitationCodeDAO()
