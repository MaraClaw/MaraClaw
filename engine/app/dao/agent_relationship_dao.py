"""DAO for agent_relationships (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.org import OrgMemberRecord

_COLUMNS = (
    "id",
    "agent_id",
    "member_id",
    "relation",
    "description",
    "created_by_user_id",
    "updated_by_user_id",
    "created_at",
    "updated_at",
)


class AgentRelationshipDAO(BaseDAO[AgentRelationshipRecord]):
    table: ClassVar[str] = "agent_relationships"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory: Any = staticmethod(AgentRelationshipRecord.from_row)

    async def list_related_user_ids(
        self,
        *,
        agent_id: UUID,
        tenant_id: UUID,
        user_ids: Sequence[UUID],
    ) -> set[UUID]:
        """Return user_ids already linked to the agent via active org members."""
        if not user_ids:
            return set()
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT m.user_id FROM org_members m "
                + "JOIN agent_relationships r ON r.member_id = m.id "
                + "WHERE r.agent_id = %(agent_id)s "
                + "AND m.tenant_id = %(tenant_id)s "
                + "AND m.status = 'active' "
                + "AND m.user_id = ANY(%(user_ids)s)",
                {"agent_id": agent_id, "tenant_id": tenant_id, "user_ids": list(user_ids)},
            )
            return {row["user_id"] for row in rows if row.get("user_id")}

    async def get_for_agent_and_member(self, agent_id: UUID, member_id: UUID) -> AgentRelationshipRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_relationships "
                + "WHERE agent_id = %(agent_id)s AND member_id = %(member_id)s LIMIT 1",
                {"agent_id": agent_id, "member_id": member_id},
            )
            return AgentRelationshipRecord.from_row(row) if row else None

    async def get_for_agent_and_user(self, agent_id: UUID, user_id: UUID) -> AgentRelationshipRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('r')} FROM agent_relationships r "
                + "JOIN org_members m ON m.id = r.member_id "
                + "WHERE r.agent_id = %(agent_id)s AND m.user_id = %(user_id)s "
                + "AND m.status = 'active' LIMIT 1",
                {"agent_id": agent_id, "user_id": user_id},
            )
            return AgentRelationshipRecord.from_row(row) if row else None

    async def list_member_ids_for_agent(self, agent_id: UUID) -> set[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT member_id FROM agent_relationships WHERE agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            return {row["member_id"] for row in rows if row.get("member_id")}

    async def list_for_agent(self, agent_id: UUID) -> Sequence[AgentRelationshipRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_relationships WHERE agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            return [AgentRelationshipRecord.from_row(row) for row in rows]

    async def get_active_for_agent_by_feishu_id(
        self, agent_id: UUID, feishu_id: str
    ) -> AgentRelationshipRecord | None:
        """Relationship + active org member matching external_id or open_id."""
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT "
                + "r.id AS r_id, r.agent_id AS r_agent_id, r.member_id AS r_member_id, "
                + "r.relation AS r_relation, r.description AS r_description, "
                + "m.id AS m_id, m.name AS m_name, m.open_id AS m_open_id, "
                + "m.unionid AS m_unionid, m.external_id AS m_external_id, "
                + "m.provider_id AS m_provider_id, m.email AS m_email, "
                + "m.phone AS m_phone, m.status AS m_status, m.tenant_id AS m_tenant_id, "
                + "m.user_id AS m_user_id, m.avatar_url AS m_avatar_url, "
                + "m.title AS m_title, m.department_id AS m_department_id, "
                + "m.department_path AS m_department_path "
                + "FROM agent_relationships r "
                + "JOIN org_members m ON m.id = r.member_id "
                + "WHERE r.agent_id = %(agent_id)s AND m.status = 'active' "
                + "AND (m.external_id = %(feishu_id)s OR m.open_id = %(feishu_id)s) "
                + "LIMIT 1",
                {"agent_id": agent_id, "feishu_id": feishu_id},
            )
            if not row:
                return None
            return _relationship_with_member(row)

    async def list_for_agent_with_members(
        self,
        agent_id: UUID,
        *,
        active_only: bool = False,
    ) -> list[AgentRelationshipRecord]:
        """Relationships for an agent, each with an attached org member record."""
        status_sql = " AND m.status = 'active'" if active_only else ""
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT "
                + "r.id AS r_id, r.agent_id AS r_agent_id, r.member_id AS r_member_id, "
                + "r.relation AS r_relation, r.description AS r_description, "
                + "r.created_by_user_id AS r_created_by_user_id, "
                + "r.updated_by_user_id AS r_updated_by_user_id, "
                + "m.id AS m_id, m.name AS m_name, m.open_id AS m_open_id, "
                + "m.unionid AS m_unionid, m.external_id AS m_external_id, "
                + "m.provider_id AS m_provider_id, m.email AS m_email, "
                + "m.phone AS m_phone, m.status AS m_status, m.tenant_id AS m_tenant_id, "
                + "m.user_id AS m_user_id, m.avatar_url AS m_avatar_url, "
                + "m.title AS m_title, m.department_id AS m_department_id, "
                + "m.department_path AS m_department_path "
                + "FROM agent_relationships r "
                + "JOIN org_members m ON m.id = r.member_id "
                + f"WHERE r.agent_id = %(agent_id)s{status_sql}",
                {"agent_id": agent_id},
            )
            return [_relationship_with_member(row) for row in rows]

    async def list_for_agent_with_members_and_providers(self, agent_id: UUID) -> list[AgentRelationshipRecord]:
        """Relationships with org member + identity provider labels for the API."""
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT "
                + "r.id AS r_id, r.agent_id AS r_agent_id, r.member_id AS r_member_id, "
                + "r.relation AS r_relation, r.description AS r_description, "
                + "r.created_by_user_id AS r_created_by_user_id, "
                + "r.updated_by_user_id AS r_updated_by_user_id, "
                + "m.id AS m_id, m.name AS m_name, m.open_id AS m_open_id, "
                + "m.unionid AS m_unionid, m.external_id AS m_external_id, "
                + "m.provider_id AS m_provider_id, m.email AS m_email, "
                + "m.phone AS m_phone, m.status AS m_status, m.tenant_id AS m_tenant_id, "
                + "m.user_id AS m_user_id, m.avatar_url AS m_avatar_url, "
                + "m.title AS m_title, m.department_id AS m_department_id, "
                + "m.department_path AS m_department_path, "
                + "p.name AS provider_name, p.provider_type AS provider_type "
                + "FROM agent_relationships r "
                + "LEFT JOIN org_members m ON m.id = r.member_id "
                + "LEFT JOIN identity_providers p ON p.id = m.provider_id "
                + "WHERE r.agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            return [_relationship_with_member(row, include_provider=True) for row in rows]

    async def delete_for_agent(self, agent_id: UUID) -> int:
        async with self.session() as db:
            rows = await db.fetchall(
                "DELETE FROM agent_relationships WHERE agent_id = %(agent_id)s RETURNING id",
                {"agent_id": agent_id},
            )
            return len(rows)

    async def get_for_agent_by_id(self, agent_id: UUID, rel_id: UUID) -> AgentRelationshipRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_relationships "
                + "WHERE id = %(rel_id)s AND agent_id = %(agent_id)s LIMIT 1",
                {"rel_id": rel_id, "agent_id": agent_id},
            )
            return AgentRelationshipRecord.from_row(row) if row else None


def _relationship_with_member(row: dict[str, Any], *, include_provider: bool = False) -> AgentRelationshipRecord:
    member: OrgMemberRecord | None = None
    if row.get("m_id") is not None:
        member = OrgMemberRecord(
            id=row["m_id"],
            name=row["m_name"],
            open_id=row.get("m_open_id"),
            unionid=row.get("m_unionid"),
            external_id=row.get("m_external_id"),
            provider_id=row.get("m_provider_id"),
            email=row.get("m_email"),
            phone=row.get("m_phone"),
            status=row.get("m_status") or "active",
            tenant_id=row.get("m_tenant_id"),
            user_id=row.get("m_user_id"),
            avatar_url=row.get("m_avatar_url"),
            title=row.get("m_title") or "",
            department_id=row.get("m_department_id"),
            department_path=row.get("m_department_path") or "",
        )
    provider_name = row.get("provider_name") if include_provider else None
    provider_type = row.get("provider_type") if include_provider else None
    return AgentRelationshipRecord(
        id=row["r_id"],
        agent_id=row["r_agent_id"],
        member_id=row["r_member_id"],
        relation=row.get("r_relation") or "collaborator",
        description=row.get("r_description") or "",
        created_by_user_id=row.get("r_created_by_user_id"),
        updated_by_user_id=row.get("r_updated_by_user_id"),
        member=member,
        provider_name=provider_name if isinstance(provider_name, str) else None,
        provider_type=provider_type if isinstance(provider_type, str) else None,
    )


agent_relationship_dao = AgentRelationshipDAO()
