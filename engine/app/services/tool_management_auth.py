"""Fresh authorization for saved configuration and remote catalog workflows."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException

from app.core.acl_locks import lock_tenants, lock_user
from app.core.security import check_token_generation, decode_access_token, get_current_user, load_user_principal
from app.db.session import get_connection
from app.records.user import UserRecord


@dataclass(frozen=True, slots=True)
class ToolActor:
    user: UserRecord
    generation: int | None


def get_tool_actor(
    user: Annotated[UserRecord, Depends(get_current_user)], authorization: Annotated[str | None, Header()] = None
) -> ToolActor:
    generation = decode_access_token(authorization.split(" ", 1)[1])["gen"] if authorization is not None else None
    return ToolActor(user, generation)


def catalog_tenant(user: UserRecord, target: UUID | None) -> UUID:
    if not user.is_platform_admin and user.role != "org_admin":
        raise HTTPException(403, "Admin access required")
    tenant_id = target if target is not None else user.tenant_id
    if tenant_id is None:
        raise HTTPException(400, "tenant_id is required")
    if not user.is_platform_admin and tenant_id != user.tenant_id:
        raise HTTPException(403, "Cannot manage another tenant's tools")
    return tenant_id


async def fresh_tool_actor(user: UserRecord, tenant_id: UUID | None, generation: int | None = None) -> UserRecord:
    """Lock scope and reload identity so authority cannot change before persistence."""
    db = get_connection()
    if db is None:
        raise RuntimeError("Tool authorization requires an owned transaction")
    await lock_tenants(db, (user.tenant_id, tenant_id))
    for target in {user.tenant_id, tenant_id} - {None}:
        tenant = await db.fetchone("SELECT is_active FROM tenants WHERE id = %(id)s", {"id": target})
        if tenant is None or not tenant["is_active"]:
            raise HTTPException(403, "Company is inactive")
    await lock_user(db, user.id, user.tenant_id)
    if user.identity_id is not None:
        await db.execute("SELECT id FROM identities WHERE id = %(id)s FOR UPDATE", {"id": user.identity_id})
    fresh = await load_user_principal(user.id, fresh=True)
    if generation is not None and fresh.identity_id is not None:
        await check_token_generation(fresh.identity_id, generation)
    if fresh.tenant_id != user.tenant_id or fresh.identity_id != user.identity_id:
        raise HTTPException(403, "User authority changed")
    return fresh
