"""DAO for agent_agent_relationships (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.agent import AgentRecord
from app.records.agent_agent_relationship import AgentAgentRelationshipRecord

_COLUMNS = (
    "id",
    "agent_id",
    "target_agent_id",
    "relation",
    "description",
    "created_by_user_id",
    "updated_by_user_id",
    "created_at",
    "updated_at",
)


class AgentAgentRelationshipDAO(BaseDAO[AgentAgentRelationshipRecord]):
    table: ClassVar[str] = "agent_agent_relationships"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory: Any = staticmethod(AgentAgentRelationshipRecord.from_row)

    async def exists(self, agent_id: UUID, target_agent_id: UUID) -> bool:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT 1 FROM agent_agent_relationships "
                + "WHERE agent_id = %(agent_id)s AND target_agent_id = %(target_agent_id)s LIMIT 1",
                {"agent_id": agent_id, "target_agent_id": target_agent_id},
            )
            return value is not None

    async def ensure(self, agent_id: UUID, target_agent_id: UUID, *, relation: str = "collaborator") -> bool:
        """Create link if missing; return True when a row was inserted."""
        if await self.exists(agent_id, target_agent_id):
            return False
        _ = await self.create(
            obj_in={
                "agent_id": agent_id,
                "target_agent_id": target_agent_id,
                "relation": relation,
                "description": "",
            }
        )
        return True

    async def list_target_agents(
        self,
        agent_id: UUID,
        *,
        exclude_system: bool = True,
        exclude_statuses: Sequence[str] | None = None,
    ) -> Sequence[AgentRecord]:
        """Agents linked as targets of agent_id, with optional filters."""
        from app.dao.agent_dao import agent_dao

        agent_cols = ", ".join(f"a.{col}" for col in agent_dao.columns)
        params: dict[str, object] = {"agent_id": agent_id}
        clauses = [
            "r.agent_id = %(agent_id)s",
        ]
        if exclude_system:
            clauses.append("a.is_system IS FALSE")
        if exclude_statuses:
            clauses.append("NOT (a.status = ANY(%(exclude_statuses)s))")
            params["exclude_statuses"] = list(exclude_statuses)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {agent_cols} FROM agents a "
                + "JOIN agent_agent_relationships r ON r.target_agent_id = a.id "
                + f"WHERE {' AND '.join(clauses)}",
                params,
            )
            return [AgentRecord.from_row(row) for row in rows]

    async def list_for_agent(self, agent_id: UUID) -> Sequence[AgentAgentRelationshipRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_agent_relationships WHERE agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            return [AgentAgentRelationshipRecord.from_row(row) for row in rows]

    async def list_for_agent_with_targets(self, agent_id: UUID) -> list[AgentAgentRelationshipRecord]:
        """Relationships for an agent with attached target agent records."""
        from app.dao.agent_dao import agent_dao

        agent_cols = ", ".join(f"a.{col} AS a_{col}" for col in agent_dao.columns)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('r')}, {agent_cols} "
                + "FROM agent_agent_relationships r "
                + "LEFT JOIN agents a ON a.id = r.target_agent_id "
                + "WHERE r.agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            return [_relationship_with_target(row, agent_dao.columns) for row in rows]

    async def delete_for_agent(self, agent_id: UUID) -> int:
        async with self.session() as db:
            rows = await db.fetchall(
                "DELETE FROM agent_agent_relationships WHERE agent_id = %(agent_id)s RETURNING id",
                {"agent_id": agent_id},
            )
            return len(rows)

    async def get_for_agent_by_id(self, agent_id: UUID, rel_id: UUID) -> AgentAgentRelationshipRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_agent_relationships "
                + "WHERE id = %(rel_id)s AND agent_id = %(agent_id)s LIMIT 1",
                {"rel_id": rel_id, "agent_id": agent_id},
            )
            return AgentAgentRelationshipRecord.from_row(row) if row else None


def _relationship_with_target(row: dict[str, Any], agent_columns: Sequence[str]) -> AgentAgentRelationshipRecord:
    target: AgentRecord | None = None
    if row.get("a_id") is not None:
        agent_row: dict[str, Any] = {col: row.get(f"a_{col}") for col in agent_columns}
        target = AgentRecord.from_row(agent_row)
    record = AgentAgentRelationshipRecord.from_row(row)
    record.target_agent = target
    return record


agent_agent_relationship_dao = AgentAgentRelationshipDAO()
