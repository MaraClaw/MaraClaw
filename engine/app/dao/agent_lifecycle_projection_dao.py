from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.db.connection import DbConnection
from app.records.agent import AgentRecord
from app.records.agent_runtime_loss import AgentRuntimeLoss
from app.records.agent_workspace_usage import AgentWorkspaceUsageRecord
from app.schemas.agent_lifecycle import AgentLifecycleStartOut


@dataclass(frozen=True, slots=True)
class CompanyRuntimePageQuery:
    company_id: UUID
    after: UUID | None
    limit: int


@dataclass(frozen=True, slots=True)
class AgentLifecycleProjections:
    starts: dict[UUID, AgentLifecycleStartOut] = field(default_factory=dict)
    losses: dict[UUID, AgentRuntimeLoss] = field(default_factory=dict)
    workspaces: dict[UUID, AgentWorkspaceUsageRecord] = field(default_factory=dict)
    upgrading: frozenset[UUID] = frozenset()
    transferring: frozenset[UUID] = frozenset()


class AgentLifecycleProjectionDAO:
    async def company_page(self, db: DbConnection, query: CompanyRuntimePageQuery) -> list[AgentRecord]:
        """One UUID keyset page plus lookahead; no per-agent reads or OFFSET."""
        boundary = " AND id > %(after)s" if query.after is not None else ""
        rows = await db.fetchall(
            "SELECT * FROM agents WHERE tenant_id=%(company_id)s" + boundary + " ORDER BY id LIMIT %(limit)s",
            {"company_id": query.company_id, "after": query.after, "limit": query.limit + 1},
        )
        return [AgentRecord.from_row(row) for row in rows]

    async def many(self, db: DbConnection, agent_ids: Sequence[UUID]) -> AgentLifecycleProjections:
        """Five fixed reads for all lifecycle concerns, independent of the number of agents."""
        if not agent_ids:
            return AgentLifecycleProjections()
        params = {"ids": list(agent_ids)}
        starts = await db.fetchall(
            "SELECT o.agent_id,o.operation_id,o.state,COALESCE(r.attempts,0) AS attempts,"
            "r.last_attempt_at,r.next_retry_at,r.last_failure_code FROM agent_start_operations o "
            "LEFT JOIN agent_start_retry_metadata r ON r.operation_id=o.operation_id WHERE o.agent_id=ANY(%(ids)s)",
            params,
        )
        losses = await db.fetchall("SELECT * FROM agent_runtime_losses WHERE agent_id=ANY(%(ids)s)", params)
        workspaces = await db.fetchall("SELECT * FROM agent_workspace_usage WHERE agent_id=ANY(%(ids)s)", params)
        upgrades = await db.fetchall(
            "SELECT agent_id FROM openclaw_update_runs WHERE agent_id=ANY(%(ids)s) AND state IN ('pending','cutover')",
            params,
        )
        transfers = await db.fetchall(
            "SELECT agent_id FROM company_transfer_runtime_cleanup WHERE agent_id=ANY(%(ids)s) AND state='pending'",
            params,
        )
        return AgentLifecycleProjections(
            starts={uuid_from_row(row["agent_id"]): AgentLifecycleStartOut.model_validate(row) for row in starts},
            losses={uuid_from_row(row["agent_id"]): AgentRuntimeLoss.from_row(row) for row in losses},
            workspaces={uuid_from_row(row["agent_id"]): AgentWorkspaceUsageRecord.from_row(row) for row in workspaces},
            upgrading=frozenset(uuid_from_row(row["agent_id"]) for row in upgrades),
            transferring=frozenset(uuid_from_row(row["agent_id"]) for row in transfers),
        )

    async def start(self, db: DbConnection, agent_id: UUID) -> AgentLifecycleStartOut | None:
        row = await db.fetchone(
            "SELECT o.operation_id,o.state,COALESCE(r.attempts,0) AS attempts,"
            "r.last_attempt_at,r.next_retry_at,r.last_failure_code FROM agent_start_operations o "
            "LEFT JOIN agent_start_retry_metadata r ON r.operation_id=o.operation_id WHERE o.agent_id=%(id)s",
            {"id": agent_id},
        )
        return AgentLifecycleStartOut.model_validate(row) if row is not None else None


agent_lifecycle_projection_dao = AgentLifecycleProjectionDAO()
