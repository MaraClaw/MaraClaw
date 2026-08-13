"""DAO for audit_logs (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.dao.base import BaseDAO


@dataclass(slots=True)
class AuditLogRecord:
    """Audit trail row."""

    id: UUID
    action: str
    user_id: UUID | None = None
    agent_id: UUID | None = None
    details: dict[str, Any] = field(default_factory=dict)
    ip_address: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AuditLogRecord:
        details = row.get("details") or {}
        if not isinstance(details, dict):
            details = dict(details)
        return cls(
            id=row["id"],
            action=row["action"],
            user_id=row.get("user_id"),
            agent_id=row.get("agent_id"),
            details=details,
            ip_address=row.get("ip_address"),
            created_at=row.get("created_at"),
        )


_COLUMNS = ("id", "user_id", "agent_id", "action", "details", "ip_address", "created_at")


class AuditLogDAO(BaseDAO[AuditLogRecord]):
    table = "audit_logs"
    columns = _COLUMNS
    record_factory = staticmethod(AuditLogRecord.from_row)

    async def list_scoped(
        self,
        *,
        tenant_id: UUID | None = None,
        agent_id: UUID | None = None,
        limit: int = 50,
    ) -> Sequence[AuditLogRecord]:
        params: dict[str, Any] = {"limit": limit}
        clauses: list[str] = []
        if tenant_id is not None:
            clauses.append("agent_id IN (SELECT id FROM agents WHERE tenant_id = %(tenant_id)s)")
            params["tenant_id"] = tenant_id
        if agent_id is not None:
            clauses.append("agent_id = %(agent_id)s")
            params["agent_id"] = agent_id
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM audit_logs {where} "
                "ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s",
                params,
            )
            return [AuditLogRecord.from_row(row) for row in rows]


audit_log_dao = AuditLogDAO()
