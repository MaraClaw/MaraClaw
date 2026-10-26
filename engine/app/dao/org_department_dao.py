"""Persistence and dynamic access joins for the org department directory."""

from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.dao.base import BaseDAO
from app.records.org_department import OrgDepartmentRecord
from app.schemas.department import AssignmentOut, AssignmentsOut, DepartmentDeletionPreviewOut, DepartmentMemberOut


@final
class OrgDepartmentDAO(BaseDAO[OrgDepartmentRecord]):
    table: ClassVar[str] = "local_departments"
    columns: ClassVar[tuple[str, ...]] = ("id", "tenant_id", "name", "created_at")
    record_factory = staticmethod(OrgDepartmentRecord.from_row)

    async def list_for_tenant(self, tenant_id: UUID) -> list[OrgDepartmentRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id, tenant_id, name, created_at FROM local_departments "
                "WHERE tenant_id = %(tenant)s ORDER BY lower(name), id",
                {"tenant": tenant_id},
            )
            return [OrgDepartmentRecord.from_row(row) for row in rows]

    async def list_assignments(self, tenant_id: UUID) -> list[AssignmentOut]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT user_id, tenant_id, department_id FROM local_department_memberships "
                "WHERE tenant_id = %(tenant)s ORDER BY user_id, department_id",
                {"tenant": tenant_id},
            )
            return [AssignmentOut.model_validate(row) for row in rows]

    async def list_members(self, tenant_id: UUID, department_id: UUID) -> list[DepartmentMemberOut]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT u.id AS membership_id, u.display_name FROM users u "
                "JOIN local_department_memberships m ON m.user_id = u.id AND m.tenant_id = u.tenant_id "
                "JOIN local_departments d ON d.id = m.department_id AND d.tenant_id = m.tenant_id "
                "LEFT JOIN identities i ON i.id = u.identity_id "
                "WHERE d.id = %(department)s AND d.tenant_id = %(tenant)s "
                "AND u.is_active IS TRUE AND u.role IN ('member', 'agent_admin') "
                "AND (i.id IS NULL OR (i.is_active IS TRUE AND i.is_platform_admin IS FALSE)) "
                "ORDER BY u.display_name, u.id",
                {"tenant": tenant_id, "department": department_id},
            )
            return [DepartmentMemberOut.model_validate(row) for row in rows]

    async def replace_assignments(self, assignment: AssignmentsOut) -> None:
        async with self.session() as db:
            await db.execute(
                "DELETE FROM local_department_memberships WHERE user_id = %(user)s AND tenant_id = %(tenant)s",
                {"user": assignment.user_id, "tenant": assignment.tenant_id},
            )
            if assignment.department_ids:
                await db.execute(
                    "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) "
                    "SELECT %(user)s, %(tenant)s, department_id FROM unnest(%(departments)s::uuid[]) AS department_id",
                    {
                        "user": assignment.user_id,
                        "tenant": assignment.tenant_id,
                        "departments": assignment.department_ids,
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

    async def member_user_ids(self, department: OrgDepartmentRecord) -> set[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT user_id FROM local_department_memberships "
                "WHERE tenant_id = %(tenant)s AND department_id = %(department)s",
                {"tenant": department.tenant_id, "department": department.id},
            )
            return {uuid_from_row(row["user_id"]) for row in rows}

    async def grant_agents_for_department(self, department: OrgDepartmentRecord) -> set[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DISTINCT a.id FROM agents a JOIN agent_permissions p ON p.agent_id = a.id "
                "WHERE a.tenant_id = %(tenant)s AND p.scope_type = 'department' AND p.scope_id = %(department)s",
                {"tenant": department.tenant_id, "department": department.id},
            )
            return {uuid_from_row(row["id"]) for row in rows}

    async def deletion_preview(self, tenant_id: UUID, department_id: UUID) -> DepartmentDeletionPreviewOut | None:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT (SELECT count(*) FROM local_department_memberships m "
                "WHERE m.tenant_id = d.tenant_id AND m.department_id = d.id) AS membership_count, "
                "(SELECT count(*) FROM agent_permissions p JOIN agents a ON a.id = p.agent_id "
                "WHERE a.tenant_id = d.tenant_id AND p.scope_type = 'department' "
                "AND p.scope_id = d.id) AS agent_grant_count "
                "FROM local_departments d WHERE d.tenant_id = %(tenant)s AND d.id = %(department)s",
                {"tenant": tenant_id, "department": department_id},
            )
            return None if row is None else DepartmentDeletionPreviewOut.model_validate(row)

    async def delete_department(self, department: OrgDepartmentRecord) -> None:
        async with self.session() as db:
            params = {"tenant": department.tenant_id, "department": department.id}
            await db.execute(
                "DELETE FROM agent_permissions p USING agents a "
                "WHERE p.agent_id = a.id AND a.tenant_id = %(tenant)s "
                "AND p.scope_type = 'department' AND p.scope_id = %(department)s",
                params,
            )
            await db.execute(
                "DELETE FROM local_department_memberships "
                "WHERE tenant_id = %(tenant)s AND department_id = %(department)s",
                params,
            )
            await db.execute(
                "DELETE FROM local_departments WHERE tenant_id = %(tenant)s AND id = %(department)s", params
            )

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


org_department_dao = OrgDepartmentDAO()
