from fastapi import HTTPException

from app.core.security import raise_if_password_change_required
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.records.skill import SkillRecord
from app.records.user import UserRecord


def ensure_skill_write_access(skill: SkillRecord, current_user: UserRecord) -> None:
    if current_user.role == "platform_admin":
        return
    if current_user.tenant_id is None:
        raise HTTPException(403, "Cannot modify skills without a tenant")
    if skill.tenant_id is not None and skill.tenant_id != current_user.tenant_id:
        raise HTTPException(403, "Cannot modify other-tenant skills")


async def fresh_import_actor(current_user: UserRecord) -> UserRecord:
    actor = await user_dao.get_with_identity(current_user.id, fresh=True)
    if actor is None or not actor.is_active or actor.identity is None or not actor.identity.is_active:
        raise HTTPException(401, "User not found or inactive")
    raise_if_password_change_required(actor)
    if actor.tenant_id != current_user.tenant_id or actor.identity_id != current_user.identity_id:
        raise HTTPException(403, "Membership changed during import")
    if actor.tenant_id is not None:
        tenants = await tenant_dao.get_many([actor.tenant_id])
        if not tenants or not tenants[0].is_active:
            raise HTTPException(403, "Tenant not found or inactive")
    return actor
