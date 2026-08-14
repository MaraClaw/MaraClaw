"""DAO for audit_logs (psycopg)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from app.core.json_types import datetime_from_row, mapping_from_row, str_from_row, uuid_from_row, uuid_from_row_opt
from app.dao.base import BaseDAO


@dataclass(slots=True)
class AuditLogRecord:
    """Audit trail row."""

    id: UUID
    action: str
    user_id: UUID | None = None
    agent_id: UUID | None = None
    details: dict[str, Any] = field(default_factory=dict[str, Any])
    ip_address: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AuditLogRecord:
        details = mapping_from_row(row.get("details") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            action=str_from_row(row["action"]),
            user_id=uuid_from_row_opt(row.get("user_id")),
            agent_id=uuid_from_row_opt(row.get("agent_id")),
            details=details,
            ip_address=str_from_row(row["ip_address"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
        )


_COLUMNS = ("id", "user_id", "agent_id", "action", "details", "ip_address", "created_at")


class AuditLogDAO(BaseDAO[AuditLogRecord]):
    table: ClassVar[str] = "audit_logs"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
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
                + "ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s",
                params,
            )
            return [AuditLogRecord.from_row(row) for row in rows]


audit_log_dao = AuditLogDAO()
