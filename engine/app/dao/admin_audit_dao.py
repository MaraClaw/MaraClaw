"""DAO for admin_audit_logs"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, final
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.audit import AdminAuditLogRecord

_COLUMNS = (
    "id",
    "actor_id",
    "actor_role",
    "actor_email",
    "action",
    "target_type",
    "target_id",
    "tenant_id",
    "changes",
    "details",
    "ip_address",
    "created_at",
)


@final
class AdminAuditLogDAO(BaseDAO[AdminAuditLogRecord]):
    table: ClassVar[str] = "admin_audit_logs"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(AdminAuditLogRecord.from_row)

    async def list_recent(
        self,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> Sequence[AdminAuditLogRecord]:
        params: dict[str, Any] = {"limit": limit}
        clauses: list[str] = []
        if tenant_id is not None:
            clauses.append("tenant_id = %(tenant_id)s")
            params["tenant_id"] = tenant_id
        if actor_id is not None:
            clauses.append("actor_id = %(actor_id)s")
            params["actor_id"] = actor_id
        if action:
            clauses.append("action = %(action)s")
            params["action"] = action
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM admin_audit_logs {where} "
                + "ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s",
                params,
            )
            return [AdminAuditLogRecord.from_row(row) for row in rows]


admin_audit_log_dao = AdminAuditLogDAO()
