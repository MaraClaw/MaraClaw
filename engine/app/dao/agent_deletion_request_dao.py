from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, json_as_bool, str_from_row, uuid_from_row
from app.dao.base import BaseDAO
from app.db.connection import DbConnection
from app.records.agent_deletion_request import AgentDeletionRequestRecord, CancelReason, DeletionStatus

# A reviewer counts only while a fresh row proves an active, password-cleared org admin of the request company.
_ELIGIBLE_SQL = (
    "u.tenant_id = {tenant} AND u.role = 'org_admin' AND u.is_active IS TRUE "
    "AND i.is_active IS TRUE AND i.must_change_password IS FALSE AND i.is_platform_admin IS FALSE "
    "AND t.is_active IS TRUE"
)
_ELIGIBLE_PARAM = _ELIGIBLE_SQL.format(tenant="%(tenant_id)s")
_REVIEWER_JOIN = (
    "JOIN users u ON u.id = {reviewer} LEFT JOIN identities i ON i.id = u.identity_id "
    "LEFT JOIN tenants t ON t.id = u.tenant_id"
)


@dataclass(frozen=True, slots=True)
class ApprovalRow:
    reviewer_id: UUID
    display_name: str
    is_genesis: bool
    approved_at: datetime
    counted: bool


@dataclass(frozen=True, slots=True)
class ReviewerRow:
    id: UUID
    display_name: str
    is_genesis: bool


@final
class AgentDeletionRequestDAO(BaseDAO[AgentDeletionRequestRecord]):
    table: ClassVar[str] = "agent_deletion_requests"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "tenant_id",
        "requester_id",
        "agent_id",
        "agent_name",
        "agent_creator_id",
        "reason",
        "status",
        "cancel_reason",
        "created_at",
        "resolved_at",
    )
    record_factory = staticmethod(AgentDeletionRequestRecord.from_row)

    async def get_locked(self, request_id: UUID) -> AgentDeletionRequestRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_deletion_requests WHERE id = %(id)s FOR UPDATE",
                {"id": request_id},
            )
            return self.record_factory(row) if row is not None else None

    async def pending_for_agent(self, agent_id: UUID) -> AgentDeletionRequestRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_deletion_requests "
                "WHERE agent_id = %(agent_id)s AND status = 'pending'",
                {"agent_id": agent_id},
            )
            return self.record_factory(row) if row is not None else None

    async def list_page(
        self,
        *,
        tenant_id: UUID,
        requester_id: UUID | None,
        status: DeletionStatus | None,
        agent_id: UUID | None,
        before: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[AgentDeletionRequestRecord]:
        params: dict[str, object] = {"tenant_id": tenant_id, "limit": limit + 1}
        clauses = ["tenant_id = %(tenant_id)s"]
        for column, value in (("requester_id", requester_id), ("status", status), ("agent_id", agent_id)):
            if value is not None:
                clauses.append(f"{column} = %({column})s")
                params[column] = value.value if isinstance(value, DeletionStatus) else value
        if before is not None:
            clauses.append("(created_at, id) < (%(created_at)s, %(before_id)s)")
            params.update(created_at=before[0], before_id=before[1])
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_deletion_requests WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
                params,
            )
            return [self.record_factory(row) for row in rows]

    async def resolve(
        self, request_id: UUID, status: DeletionStatus, cancel_reason: CancelReason | None = None
    ) -> AgentDeletionRequestRecord:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE agent_deletion_requests SET status = %(status)s, cancel_reason = %(cancel)s, "
                f"resolved_at = now() WHERE id = %(id)s RETURNING {self._select_list()}",
                {
                    "id": request_id,
                    "status": status.value,
                    "cancel": cancel_reason.value if cancel_reason is not None else None,
                },
            )
            if row is None:
                raise RuntimeError("Agent deletion request disappeared while locked")
            return self.record_factory(row)

    async def pending_for_agents_locked(self, agent_ids: Sequence[UUID]) -> list[AgentDeletionRequestRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_deletion_requests "
                "WHERE agent_id = ANY(%(ids)s) AND status = 'pending' ORDER BY id FOR UPDATE",
                {"ids": list(agent_ids)},
            )
            return [self.record_factory(row) for row in rows]

    async def lock_users(self, db: DbConnection, user_ids: Sequence[UUID]) -> None:
        for user_id in sorted(set(user_ids)):
            await db.fetchone("SELECT id FROM users WHERE id = %(id)s FOR UPDATE", {"id": user_id})

    async def lock_agent(self, db: DbConnection, agent_id: UUID) -> bool:
        return await db.fetchone("SELECT id FROM agents WHERE id = %(id)s FOR UPDATE", {"id": agent_id}) is not None

    async def eligible_reviewer(self, tenant_id: UUID, user_id: UUID) -> ReviewerRow | None:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT u.id, u.display_name, u.is_genesis FROM users u "
                "LEFT JOIN identities i ON i.id = u.identity_id LEFT JOIN tenants t ON t.id = u.tenant_id "
                f"WHERE u.id = %(user_id)s AND {_ELIGIBLE_PARAM}",
                {"tenant_id": tenant_id, "user_id": user_id},
            )
            return _reviewer(row) if row is not None else None

    async def eligible_reviewers(self, tenant_id: UUID) -> list[ReviewerRow]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT u.id, u.display_name, u.is_genesis FROM users u "
                "LEFT JOIN identities i ON i.id = u.identity_id LEFT JOIN tenants t ON t.id = u.tenant_id "
                f"WHERE {_ELIGIBLE_PARAM} ORDER BY u.id",
                {"tenant_id": tenant_id},
            )
            return [_reviewer(row) for row in rows]

    async def progress(self, request_ids: Sequence[UUID]) -> dict[UUID, tuple[int, bool]]:
        if not request_ids:
            return {}
        eligible = _ELIGIBLE_SQL.format(tenant="r.tenant_id")
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT a.request_id, COUNT(*) FILTER (WHERE COALESCE((" + eligible + "), FALSE)) AS counted, "
                "COALESCE(BOOL_OR((" + eligible + ") AND u.is_genesis), FALSE) AS genesis "
                "FROM agent_deletion_request_approvals a JOIN agent_deletion_requests r ON r.id = a.request_id "
                "JOIN users u ON u.id = a.reviewer_id LEFT JOIN identities i ON i.id = u.identity_id "
                "LEFT JOIN tenants t ON t.id = u.tenant_id WHERE a.request_id = ANY(%(ids)s) GROUP BY a.request_id",
                {"ids": list(request_ids)},
            )
            return {
                uuid_from_row(row["request_id"]): (int_from_row(row["counted"]), json_as_bool(row["genesis"]))
                for row in rows
            }

    async def record_view(self, request_id: UUID, reviewer_id: UUID) -> None:
        async with self.session() as db:
            await db.execute(
                "INSERT INTO agent_deletion_request_views (request_id, reviewer_id) "
                "VALUES (%(request_id)s, %(reviewer_id)s) ON CONFLICT DO NOTHING",
                {"request_id": request_id, "reviewer_id": reviewer_id},
            )

    async def has_viewed(self, request_id: UUID, reviewer_id: UUID) -> bool:
        async with self.session() as db:
            return bool(
                await db.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM agent_deletion_request_views "
                    "WHERE request_id = %(request_id)s AND reviewer_id = %(reviewer_id)s)",
                    {"request_id": request_id, "reviewer_id": reviewer_id},
                )
            )

    async def record_approval(self, request_id: UUID, reviewer_id: UUID) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                "INSERT INTO agent_deletion_request_approvals (request_id, reviewer_id) "
                "VALUES (%(request_id)s, %(reviewer_id)s) ON CONFLICT DO NOTHING RETURNING reviewer_id",
                {"request_id": request_id, "reviewer_id": reviewer_id},
            )
            return row is not None

    async def approvals(self, request: AgentDeletionRequestRecord) -> list[ApprovalRow]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT a.reviewer_id, a.approved_at, u.display_name, (u.is_genesis AND u.role = 'org_admin') "
                f"AS is_genesis, COALESCE(({_ELIGIBLE_PARAM}), FALSE) AS counted "
                "FROM agent_deletion_request_approvals a "
                + _REVIEWER_JOIN.format(reviewer="a.reviewer_id")
                + " WHERE a.request_id = %(request_id)s ORDER BY a.approved_at, a.reviewer_id",
                {"request_id": request.id, "tenant_id": request.tenant_id},
            )
            return [
                ApprovalRow(
                    reviewer_id=uuid_from_row(row["reviewer_id"]),
                    display_name=str_from_row(row["display_name"]),
                    is_genesis=json_as_bool(row["is_genesis"]),
                    approved_at=_required_time(row["approved_at"]),
                    counted=json_as_bool(row["counted"]),
                )
                for row in rows
            ]


def _reviewer(row: dict[str, object]) -> ReviewerRow:
    return ReviewerRow(uuid_from_row(row["id"]), str_from_row(row["display_name"]), json_as_bool(row["is_genesis"]))


def _required_time(value: object) -> datetime:
    parsed = datetime_from_row(value)
    if parsed is None:
        raise ValueError("Approval timestamp is required")
    return parsed


agent_deletion_request_dao = AgentDeletionRequestDAO()
