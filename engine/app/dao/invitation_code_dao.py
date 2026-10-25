"""DAO for invitation_codes table"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import int_from_row
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
    "expires_at",
    "invited_email",
    "redeemed_at",
    "redeemed_by",
)

# Unexpired, active, org-owned, and bound to the presented (normalized) recipient. A missing email matches nothing.
_REDEEMABLE = (
    "WHERE code = %(code)s AND is_active IS TRUE AND tenant_id IS NOT NULL AND expires_at > now() "
    "AND invited_email = %(email)s"
)


@final
class InvitationCodeDAO(BaseDAO[InvitationCodeRecord]):
    """DAO for InvitationCode records."""

    table: ClassVar[str] = "invitation_codes"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(InvitationCodeRecord.from_row)

    async def get_active_by_code(self, code: str, *, email: str | None = None) -> InvitationCodeRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM invitation_codes {_REDEEMABLE} LIMIT 1",
                {"code": code, "email": email},
            )
            return InvitationCodeRecord.from_row(row) if row is not None else None

    async def lock_redeemable(self, code: str, *, email: str | None) -> InvitationCodeRecord | None:
        """Row-lock a code that can still be redeemed by ``email`` (normalized)."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM invitation_codes {_REDEEMABLE} "
                "AND used_count < max_uses FOR UPDATE",
                {"code": code, "email": email},
            )
            return InvitationCodeRecord.from_row(row) if row is not None else None

    async def consume(self, code: str, *, user_id: UUID | None, email: str | None) -> bool:
        """Atomically spend one use; False when it is expired, revoked, exhausted or bound elsewhere."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE invitation_codes SET used_count = used_count + 1, redeemed_at = now(), "
                f"redeemed_by = %(user)s {_REDEEMABLE} AND used_count < max_uses RETURNING id",
                {"code": code, "email": email, "user": user_id},
            )
            return row is not None

    async def get_for_tenant(self, code_id: UUID, tenant_id: UUID) -> InvitationCodeRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM invitation_codes "
                "WHERE id = %(id)s AND tenant_id = %(tenant_id)s LIMIT 1",
                {"id": code_id, "tenant_id": tenant_id},
            )
            return InvitationCodeRecord.from_row(row) if row is not None else None

    async def count_for_tenant(self, tenant_id: UUID, *, search: str | None = None) -> int:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        search_sql = ""
        if search not in (None, ""):
            params["search"] = f"%{search}%"
            search_sql = " AND code ILIKE %(search)s"
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT COUNT(*) FROM invitation_codes WHERE tenant_id = %(tenant_id)s{search_sql}",
                params,
            )
            return int_from_row(value)

    async def list_for_tenant(
        self,
        tenant_id: UUID,
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
        if search not in (None, ""):
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

    async def list_all_for_tenant(self, tenant_id: UUID) -> Sequence[InvitationCodeRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM invitation_codes "
                "WHERE tenant_id = %(tenant_id)s ORDER BY created_at ASC NULLS LAST",
                {"tenant_id": tenant_id},
            )
            return [InvitationCodeRecord.from_row(row) for row in rows]


invitation_code_dao = InvitationCodeDAO()
