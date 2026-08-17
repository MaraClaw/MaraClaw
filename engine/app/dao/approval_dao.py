"""DAO for approval_requests"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar, final
from uuid import UUID, uuid4

from app.core.json_types import int_from_row
from app.dao.base import BaseDAO
from app.db.types import as_jsonb
from app.records.audit import ApprovalRequestRecord

_COLUMNS = (
    "id",
    "agent_id",
    "action_type",
    "details",
    "status",
    "created_at",
    "resolved_at",
    "resolved_by",
)


@final
class ApprovalRequestDAO(BaseDAO[ApprovalRequestRecord]):
    """DAO for L3 autonomy approval requests."""

    table: ClassVar[str] = "approval_requests"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(ApprovalRequestRecord.from_row)

    async def create_pending(
        self,
        *,
        agent_id: UUID,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> ApprovalRequestRecord:
        """Insert a pending approval and return the created row."""
        approval_id = uuid4()
        async with self.session() as db:
            row = await db.fetchone(
                "INSERT INTO approval_requests "
                + "(id, agent_id, action_type, details, status, created_at) "
                + "VALUES (%(id)s, %(agent_id)s, %(action_type)s, %(details)s, 'pending', %(created_at)s) "
                + f"RETURNING {self._select_list()}",
                {
                    "id": approval_id,
                    "agent_id": agent_id,
                    "action_type": action_type,
                    "details": as_jsonb(details or {}),
                    "created_at": datetime.now(UTC),
                },
            )
            if row is None:
                raise RuntimeError("INSERT into approval_requests returned no row")
            return ApprovalRequestRecord.from_row(row)

    async def resolve(
        self,
        approval: ApprovalRequestRecord,
        *,
        status: str,
        resolved_by: UUID,
    ) -> ApprovalRequestRecord:
        """Mark an approval approved/rejected and return the refreshed row."""
        resolved_at = datetime.now(UTC)
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE approval_requests SET status = %(status)s, "
                + "resolved_at = %(resolved_at)s, resolved_by = %(resolved_by)s "
                + f"WHERE id = %(id)s RETURNING {self._select_list()}",
                {
                    "id": approval.id,
                    "status": status,
                    "resolved_at": resolved_at,
                    "resolved_by": resolved_by,
                },
            )
            if row is None:
                return ApprovalRequestRecord(
                    id=approval.id,
                    agent_id=approval.agent_id,
                    action_type=approval.action_type,
                    details=approval.details,
                    status=status,
                    created_at=approval.created_at,
                    resolved_at=resolved_at,
                    resolved_by=resolved_by,
                )
            return ApprovalRequestRecord.from_row(row)

    async def list_for_agent(
        self,
        agent_id: UUID,
        *,
        status: str | None = None,
    ) -> Sequence[ApprovalRequestRecord]:
        params: dict[str, Any] = {"agent_id": agent_id}
        status_sql = ""
        if status:
            status_sql = " AND status = %(status)s"
            params["status"] = status
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM approval_requests "
                + f"WHERE agent_id = %(agent_id)s{status_sql} "
                + "ORDER BY created_at DESC NULLS LAST",
                params,
            )
            return [ApprovalRequestRecord.from_row(row) for row in rows]

    async def list_scoped(
        self,
        *,
        tenant_id: UUID | None = None,
        creator_id: UUID | None = None,
        status: str | None = None,
    ) -> Sequence[ApprovalRequestRecord]:
        """List approvals optionally scoped to tenant agents and/or creator."""
        params: dict[str, Any] = {}
        clauses: list[str] = []
        if tenant_id is not None:
            clauses.append("agent_id IN (SELECT id FROM agents WHERE tenant_id = %(tenant_id)s)")
            params["tenant_id"] = tenant_id
        if creator_id is not None:
            clauses.append("agent_id IN (SELECT id FROM agents WHERE creator_id = %(creator_id)s)")
            params["creator_id"] = creator_id
        if status:
            clauses.append("status = %(status)s")
            params["status"] = status
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM approval_requests {where} ORDER BY created_at DESC NULLS LAST",
                params or None,
            )
            return [ApprovalRequestRecord.from_row(row) for row in rows]

    async def count_pending(self, *, tenant_id: UUID | None = None) -> int:
        params: dict[str, Any] = {}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND agent_id IN (SELECT id FROM agents WHERE tenant_id = %(tenant_id)s)"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'{tenant_sql}",
                params or None,
            )
            return int_from_row(value)


approval_request_dao = ApprovalRequestDAO()
