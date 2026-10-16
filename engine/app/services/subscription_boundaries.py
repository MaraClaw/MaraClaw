from uuid import UUID

from fastapi import HTTPException

from app.core.security import raise_if_password_change_required
from app.dao.llm_dao import llm_model_dao
from app.dao.user_dao import user_dao
from app.db.session import get_connection
from app.records.llm import LLMModelRecord
from app.records.user import UserRecord
from app.services.enterprise_llm import assert_can_manage_model, assert_llm_pool_admin


async def fresh_subscription_actor(user: UserRecord) -> UserRecord:
    db = get_connection()
    if db is None:
        return user
    if user.tenant_id is not None:
        await active_subscription_tenant(user.tenant_id)
    await db.execute("SELECT id FROM users WHERE id = %(id)s FOR UPDATE", {"id": user.id})
    fresh = await user_dao.get_with_identity(user.id, fresh=True)
    if (
        fresh is None
        or not fresh.is_active
        or fresh.tenant_id != user.tenant_id
        or fresh.identity_id != user.identity_id
    ):
        raise HTTPException(status_code=403, detail="Administrator authority changed")
    if fresh.identity_id is not None:
        await db.execute("SELECT id FROM identities WHERE id = %(id)s FOR UPDATE", {"id": fresh.identity_id})
        fresh = await user_dao.get_with_identity(user.id, fresh=True)
    if (
        fresh is None
        or not fresh.is_active
        or fresh.tenant_id != user.tenant_id
        or fresh.identity_id != user.identity_id
        or fresh.identity is None
        or not fresh.identity.is_active
    ):
        raise HTTPException(status_code=403, detail="Identity is inactive")
    raise_if_password_change_required(fresh)
    assert_llm_pool_admin(fresh)
    return fresh


async def active_subscription_tenant(tenant_id: UUID) -> None:
    db = get_connection()
    if db is None:
        return
    tenant = await db.fetchone("SELECT is_active FROM tenants WHERE id = %(id)s FOR UPDATE", {"id": tenant_id})
    if tenant is None or not tenant["is_active"]:
        raise HTTPException(status_code=403, detail="Company is inactive")


async def unchanged_subscription_model(snapshot: LLMModelRecord, actor: UserRecord | None = None) -> LLMModelRecord:
    db = get_connection()
    if db is None:
        return snapshot
    if actor is not None:
        actor = await fresh_subscription_actor(actor)
    if snapshot.tenant_id is not None and (actor is None or snapshot.tenant_id != actor.tenant_id):
        await active_subscription_tenant(snapshot.tenant_id)
    await db.execute("SELECT id FROM llm_models WHERE id = %(id)s FOR UPDATE", {"id": snapshot.id})
    current = await llm_model_dao.get(snapshot.id)
    if current is None:
        raise HTTPException(status_code=404, detail="Model not found")
    if actor is not None:
        assert_can_manage_model(actor, current)
    if current != snapshot:
        raise HTTPException(status_code=409, detail="Model changed during subscription request; retry")
    return current
