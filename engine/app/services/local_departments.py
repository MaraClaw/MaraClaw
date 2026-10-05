"""Local directory administration, isolated from identity-provider sync."""

from uuid import UUID

from fastapi import HTTPException

from app.core.access_cache import bump_agent_acl_version
from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.security import raise_if_password_change_required
from app.dao.local_department_dao import local_department_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.user import UserRecord
from app.schemas.department import AssignmentOut
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


async def assign_department(actor: UserRecord, assignment: AssignmentOut) -> AssignmentOut:
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
        if subject is None or subject.role not in END_USER_ROLES:
            raise HTTPException(422, "Only end users may have a department")
        if assignment.department_id is not None:
            department = await local_department_dao.get(assignment.department_id)
            if department is None or department.tenant_id != assignment.tenant_id:
                raise HTTPException(422, "Invalid department for this organization")
        affected = await local_department_dao.grant_agents_for_user(subject.id)
        await local_department_dao.assign(assignment)
        affected.update(await local_department_dao.grant_agents_for_user(subject.id))
        for agent_id in sorted(affected):
            await lock_agent(db, agent_id, assignment.tenant_id)
            await bump_agent_acl_version(agent_id)
    return assignment
