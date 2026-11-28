"""DAO for audit_logs"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, final
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
    details: dict[str, object] = field(default_factory=dict[str, object])
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


@final
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
        before: tuple[datetime, UUID] | None = None,
    ) -> Sequence[AuditLogRecord]:
        params: dict[str, object] = {"limit": limit}
        clauses: list[str] = []
        if tenant_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM agents WHERE id = %(agent_id)s AND tenant_id = %(tenant_id)s)"
                if agent_id is not None
                else "agent_id IN (SELECT id FROM agents WHERE tenant_id = %(tenant_id)s)"
            )
            params["tenant_id"] = tenant_id
        if agent_id is not None:
            clauses.append("agent_id = %(agent_id)s")
            params["agent_id"] = agent_id
        if before is not None:
            clauses.append("(created_at, id) < (%(before_created_at)s, %(before_id)s)")
            params["before_created_at"], params["before_id"] = before
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM audit_logs {where} "
                "ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
                params,
            )
            return [AuditLogRecord.from_row(row) for row in rows]

    async def purge_older_than(self, *, older_than: datetime, limit: int) -> int:
        """Delete one index-ordered batch of old audit rows, skipping locks held by other transactions."""
        async with self.session() as db:
            rows = await db.fetchall(
                """
                WITH expired AS (
                    SELECT ctid FROM audit_logs
                    WHERE created_at < %(cutoff)s
                    ORDER BY created_at LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                ) DELETE FROM audit_logs WHERE ctid = ANY(ARRAY(SELECT ctid FROM expired))
                  RETURNING id
                """,
                {"cutoff": older_than, "limit": limit},
            )
            return len(rows)


audit_log_dao = AuditLogDAO()
