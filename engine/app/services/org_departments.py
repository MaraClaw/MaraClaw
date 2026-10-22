"""Org department directory administration and membership assignment."""

from uuid import UUID

from fastapi import HTTPException

from app.core.access_cache import bump_agent_acl_version
from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.security import raise_if_password_change_required
from app.dao.org_department_dao import org_department_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.errors import UniqueViolationError
from app.db.session import connection_ctx
from app.records.user import UserRecord
from app.schemas.department import AssignmentOut, AssignmentsOut, DepartmentCreate, DepartmentMembersOut, DepartmentOut
from app.services.admin_audit import field_change, write_admin_audit
from app.services.admin_provisioning import END_USER_ROLES


def admin_tenant(user: UserRecord, selected: UUID | None) -> UUID:
    if user.role == "platform_admin":
        if selected is None:
            raise HTTPException(422, "Select an organization")
        return selected
    if user.role != "org_admin" or user.tenant_id is None:
        raise HTTPException(403, "Organization administrator required")
    if selected is not None and selected != user.tenant_id:
        raise HTTPException(403, "Organization access denied")
    return user.tenant_id


async def list_department_members(actor: UserRecord, department_id: UUID) -> DepartmentMembersOut:
    if (
        not actor.is_active
        or actor.is_platform_admin
        or actor.tenant_id is None
        or actor.role not in (*END_USER_ROLES, "org_admin")
        or (actor.identity is not None and not actor.identity.is_active)
    ):
        raise HTTPException(403, "Organization membership required")
    raise_if_password_change_required(actor)
    department = await org_department_dao.get(department_id)
    if department is None or department.tenant_id != actor.tenant_id:
        raise HTTPException(404, "Department not found")
    return DepartmentMembersOut(members=await org_department_dao.list_members(actor.tenant_id, department_id))


async def rename_department(
    actor: UserRecord, department_id: UUID, data: DepartmentCreate, tenant_id: UUID | None
) -> DepartmentOut:
    tenant = admin_tenant(actor, tenant_id)
    try:
        async with connection_ctx() as db:
            await lock_tenants(db, (tenant, actor.tenant_id))
            await lock_user(db, actor.id, actor.tenant_id)
            fresh_actor = await user_dao.get_with_identity(actor.id, fresh=True)
            if fresh_actor is None or not fresh_actor.is_active:
                raise HTTPException(403, "Administrator is inactive")
            raise_if_password_change_required(fresh_actor)
            admin_tenant(fresh_actor, tenant)
            if await tenant_dao.get(tenant) is None:
                raise HTTPException(404, "Organization not found")
            department = await org_department_dao.get(department_id)
            if department is None or department.tenant_id != tenant:
                raise HTTPException(404, "Department not found")
            before_name = department.name
            if before_name == data.name:
                return DepartmentOut.model_validate(department)
            updated = await org_department_dao.update(db_obj=department, obj_in={"name": data.name})
            await write_admin_audit(
                actor=fresh_actor,
                action="department.rename",
                target_type="department",
                target_id=department_id,
                tenant_id=tenant,
                changes={"name": field_change(before_name, updated.name)},
            )
            return DepartmentOut.model_validate(updated)
    except UniqueViolationError as exc:
        raise HTTPException(409, "Department name already exists") from exc


async def assign_department(actor: UserRecord, assignment: AssignmentOut) -> AssignmentOut:
    await assign_departments(
        actor,
        AssignmentsOut(
            user_id=assignment.user_id,
            tenant_id=assignment.tenant_id,
            department_ids=[] if assignment.department_id is None else [assignment.department_id],
        ),
    )
    return assignment


async def delete_department(actor: UserRecord, department_id: UUID, tenant_id: UUID | None) -> None:
    tenant = admin_tenant(actor, tenant_id)
    async with connection_ctx() as db:
        await lock_tenants(db, (tenant, actor.tenant_id))
        await lock_user(db, actor.id, actor.tenant_id)
        fresh_actor = await user_dao.get_with_identity(actor.id, fresh=True)
        if (
            fresh_actor is None
            or not fresh_actor.is_active
            or (fresh_actor.identity is not None and not fresh_actor.identity.is_active)
        ):
            raise HTTPException(403, "Administrator is inactive")
        raise_if_password_change_required(fresh_actor)
        admin_tenant(fresh_actor, tenant)
        if await tenant_dao.get(tenant) is None:
            raise HTTPException(404, "Organization not found")
        department = await org_department_dao.get(department_id)
        if department is None or department.tenant_id != tenant:
            raise HTTPException(404, "Department not found")
        before_name = department.name
        for user_id in sorted(await org_department_dao.member_user_ids(department)):
            if user_id != actor.id:
                await lock_user(db, user_id, tenant)
        affected = await org_department_dao.grant_agents_for_department(department)
        for agent_id in sorted(affected):
            await lock_agent(db, agent_id, tenant)
        await org_department_dao.delete_department(department)
        for agent_id in sorted(affected):
            await bump_agent_acl_version(agent_id)
        await write_admin_audit(
            actor=fresh_actor,
            action="department.delete",
            target_type="department",
            target_id=department_id,
            tenant_id=tenant,
            changes={"name": field_change(before_name, None)},
        )


async def assign_departments(actor: UserRecord, assignment: AssignmentsOut) -> AssignmentsOut:
    async with connection_ctx() as db:
        await lock_tenants(db, (assignment.tenant_id, actor.tenant_id))
        await lock_user(db, actor.id, actor.tenant_id)
        fresh_actor = await user_dao.get_with_identity(actor.id, fresh=True)
        if fresh_actor is None or not fresh_actor.is_active:
            raise HTTPException(403, "Administrator is inactive")
        raise_if_password_change_required(fresh_actor)
        admin_tenant(fresh_actor, assignment.tenant_id)
        subject = await user_dao.get(assignment.user_id)
        if subject is None:
            raise HTTPException(404, "User not found")
        if subject.tenant_id != assignment.tenant_id:
            raise HTTPException(403, "User belongs to another organization")
        await lock_user(db, subject.id, assignment.tenant_id)
        subject = await user_dao.get(subject.id)
        if subject is None:
            raise HTTPException(404, "User not found")
        if subject.tenant_id != assignment.tenant_id:
            raise HTTPException(403, "User belongs to another organization")
        if subject.role not in END_USER_ROLES:
            raise HTTPException(422, "Only end users may have departments")
        departments = await org_department_dao.get_many(assignment.department_ids)
        if len(departments) != len(assignment.department_ids) or any(
            department.tenant_id != assignment.tenant_id for department in departments
        ):
            raise HTTPException(422, "Invalid department for this organization")
        before_ids = sorted(
            {
                row.department_id
                for row in await org_department_dao.list_assignments(assignment.tenant_id)
                if row.user_id == subject.id and row.department_id is not None
            }
        )
        if before_ids == assignment.department_ids:
            return assignment
        affected = await org_department_dao.grant_agents_for_user(subject.id)
        await org_department_dao.replace_assignments(assignment)
        affected.update(await org_department_dao.grant_agents_for_user(subject.id))
        for agent_id in sorted(affected):
            await lock_agent(db, agent_id, assignment.tenant_id)
            await bump_agent_acl_version(agent_id)
        await write_admin_audit(
            actor=fresh_actor,
            action="department.membership.replace",
            target_type="user",
            target_id=subject.id,
            tenant_id=assignment.tenant_id,
            changes={
                "department_ids": field_change(
                    [str(department_id) for department_id in before_ids],
                    [str(department_id) for department_id in assignment.department_ids],
                )
            },
        )
    return assignment
