"""DAO for sso_scan_sessions"""

from __future__ import annotations

from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import uuid_from_row_opt
from app.dao.base import BaseDAO
from app.records.sso_scan_session import SSOScanSessionRecord

_COLUMNS = (
    "id",
    "status",
    "provider_type",
    "error_msg",
    "tenant_id",
    "user_id",
    "access_token",
    "expires_at",
    "created_at",
)


@final
class SSOScanSessionDAO(BaseDAO[SSOScanSessionRecord]):
    table: ClassVar[str] = "sso_scan_sessions"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(SSOScanSessionRecord.from_row)

    async def authorize(
        self, session_id: UUID, *, user_id: UUID, provider_type: str, tenant_id: UUID | None
    ) -> SSOScanSessionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE sso_scan_sessions SET status = 'authorized', user_id = %(user_id)s, "
                + "provider_type = %(provider_type)s WHERE id = %(session_id)s "
                + "AND status IN ('pending', 'scanned') AND expires_at > now() "
                + "AND (tenant_id IS NULL OR tenant_id = %(tenant_id)s) "
                + f"RETURNING {self._select_list()}",
                {"session_id": session_id, "user_id": user_id, "provider_type": provider_type, "tenant_id": tenant_id},
            )
        return self.record_factory(row) if row is not None else None

    async def redeem(self, session_id: UUID) -> UUID | None:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE sso_scan_sessions SET status = 'completed' WHERE id = %(session_id)s "
                + "AND status = 'authorized' AND expires_at > now() RETURNING user_id",
                {"session_id": session_id},
            )
        return uuid_from_row_opt(row["user_id"]) if row is not None else None

    async def expire(self, session_id: UUID) -> SSOScanSessionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE sso_scan_sessions SET status = 'expired' WHERE id = %(session_id)s "
                + "AND status IN ('pending', 'scanned', 'authorized') AND expires_at <= now() "
                + f"RETURNING {self._select_list()}",
                {"session_id": session_id},
            )
        return self.record_factory(row) if row is not None else None

    async def mark_scanned(self, session_id: UUID) -> SSOScanSessionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE sso_scan_sessions SET status = 'scanned' WHERE id = %(session_id)s "
                + "AND status = 'pending' AND expires_at > now() "
                + f"RETURNING {self._select_list()}",
                {"session_id": session_id},
            )
        return self.record_factory(row) if row is not None else None


sso_scan_session_dao = SSOScanSessionDAO()
