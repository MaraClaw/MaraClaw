"""Persistence and dynamic access joins for the local department directory."""

from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.dao.base import BaseDAO
from app.records.local_department import LocalDepartmentRecord
from app.schemas.department import AssignmentOut


@final
class LocalDepartmentDAO(BaseDAO[LocalDepartmentRecord]):
    table: ClassVar[str] = "local_departments"
    columns: ClassVar[tuple[str, ...]] = ("id", "tenant_id", "name", "created_at")
    record_factory = staticmethod(LocalDepartmentRecord.from_row)

    async def list_for_tenant(self, tenant_id: UUID) -> list[LocalDepartmentRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id, tenant_id, name, created_at FROM local_departments "
                "WHERE tenant_id = %(tenant)s ORDER BY lower(name), id",
                {"tenant": tenant_id},
            )
            return [LocalDepartmentRecord.from_row(row) for row in rows]

    async def list_assignments(self, tenant_id: UUID) -> list[AssignmentOut]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT user_id, tenant_id, department_id FROM local_department_memberships "
                "WHERE tenant_id = %(tenant)s ORDER BY user_id",
                {"tenant": tenant_id},
            )
            return [AssignmentOut.model_validate(row) for row in rows]

    async def assign(self, assignment: AssignmentOut) -> None:
        async with self.session() as db:
            if assignment.department_id is None:
                await db.execute(
                    "DELETE FROM local_department_memberships WHERE user_id = %(user)s", {"user": assignment.user_id}
                )
            else:
                await db.execute(
                    "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) "
                    "VALUES (%(user)s, %(tenant)s, %(department)s) ON CONFLICT (user_id) DO UPDATE "
                    "SET tenant_id = EXCLUDED.tenant_id, department_id = EXCLUDED.department_id, updated_at = now()",
                    {
                        "user": assignment.user_id,
                        "tenant": assignment.tenant_id,
                        "department": assignment.department_id,
                    },
                )

    async def grant_agents_for_user(self, user_id: UUID) -> set[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DISTINCT a.id FROM local_department_memberships m "
                "JOIN agent_permissions p ON p.scope_type = 'department' AND p.scope_id = m.department_id "
                "JOIN agents a ON a.id = p.agent_id AND a.tenant_id = m.tenant_id "
                "WHERE m.user_id = %(user)s",
                {"user": user_id},
            )
            return {uuid_from_row(row["id"]) for row in rows}

    async def has_agent_access(self, user_id: UUID, agent_id: UUID) -> bool:
        async with self.session() as db:
            return bool(
                await db.fetchval(
                    "SELECT 1 FROM users u JOIN local_department_memberships m "
                    "ON m.user_id = u.id AND m.tenant_id = u.tenant_id "
                    "JOIN local_departments d ON d.id = m.department_id AND d.tenant_id = m.tenant_id "
                    "JOIN agent_permissions p ON p.scope_type = 'department' AND p.scope_id = d.id "
                    "AND p.access_level = 'use' JOIN agents a ON a.id = p.agent_id AND a.tenant_id = u.tenant_id "
                    "WHERE u.id = %(user)s AND u.is_active IS TRUE AND a.id = %(agent)s "
                    "AND a.access_mode = 'custom' LIMIT 1",
                    {"user": user_id, "agent": agent_id},
                )
            )

    async def accessible_user_ids(self, agent_id: UUID) -> set[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DISTINCT u.id FROM users u JOIN local_department_memberships m "
                "ON m.user_id = u.id AND m.tenant_id = u.tenant_id "
                "JOIN local_departments d ON d.id = m.department_id AND d.tenant_id = m.tenant_id "
                "JOIN agent_permissions p ON p.scope_type = 'department' AND p.scope_id = d.id "
                "AND p.access_level = 'use' JOIN agents a ON a.id = p.agent_id AND a.tenant_id = u.tenant_id "
                "WHERE a.id = %(agent)s AND a.access_mode = 'custom' AND u.is_active IS TRUE",
                {"agent": agent_id},
            )
            return {uuid_from_row(row["id"]) for row in rows}


local_department_dao = LocalDepartmentDAO()
