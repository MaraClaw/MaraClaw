"""DAO for admin_audit_logs"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
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
        if action not in (None, ""):
            clauses.append("action = %(action)s")
            params["action"] = action
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM admin_audit_logs {where} "
                "ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s",
                params,
            )
            return [AdminAuditLogRecord.from_row(row) for row in rows]

    async def list_page(
        self,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
        action: str | None = None,
        limit: int = 100,
        before: tuple[datetime, UUID] | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> Sequence[AdminAuditLogRecord]:
        """Fetch a bounded seek page plus one lookahead row; never use an offset."""
        params: dict[str, object] = {"limit": min(max(limit, 1), 500) + 1}
        clauses: list[str] = []
        if tenant_id is not None:
            clauses.append("tenant_id = %(tenant_id)s")
            params["tenant_id"] = tenant_id
        if actor_id is not None:
            clauses.append("actor_id = %(actor_id)s")
            params["actor_id"] = actor_id
        if action not in (None, ""):
            clauses.append("action = %(action)s")
            params["action"] = action
        if before is not None:
            clauses.append("(created_at, id) < (%(before_created_at)s, %(before_id)s)")
            params["before_created_at"], params["before_id"] = before
        if start_at is not None:
            clauses.append("created_at >= %(start_at)s")
            params["start_at"] = start_at
        if end_at is not None:
            clauses.append("created_at < %(end_at)s")
            params["end_at"] = end_at
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM admin_audit_logs {where} "
                "ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
                params,
            )
            return [AdminAuditLogRecord.from_row(row) for row in rows]


admin_audit_log_dao = AdminAuditLogDAO()
