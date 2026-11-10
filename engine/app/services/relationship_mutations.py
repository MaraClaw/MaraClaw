"""Transactional authorization and member eligibility for human network writes."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel

from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.permissions import check_agent_access, compute_agent_access_level
from app.core.security import raise_if_password_change_required
from app.dao import org_member_dao, user_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.user import UserRecord


class RelationshipIn(BaseModel):
    member_id: str
    relation: str = "collaborator"
    description: str = ""


@asynccontextmanager
async def human_relationship_mutation(agent_id: UUID, actor: UserRecord) -> AsyncGenerator[AgentRecord]:
    """Reauthorize after tenant, actor and agent locks, retaining them through every write."""
    initial, _ = await check_agent_access(actor, agent_id)
    async with connection_ctx() as db:
        await lock_tenants(db, (initial.tenant_id, actor.tenant_id))
        await lock_user(db, actor.id, actor.tenant_id)
        await lock_agent(db, agent_id, initial.tenant_id)
        fresh = await user_dao.get_with_identity(actor.id, fresh=True)
        if fresh is None or not fresh.is_active or (fresh.identity is not None and not fresh.identity.is_active):
            raise HTTPException(403, "User is not available")
        raise_if_password_change_required(fresh)
        agent, access = await check_agent_access(fresh, agent_id, fresh=True)
        if access != "manage" and fresh.role not in ("org_admin", "platform_admin"):
            raise HTTPException(403, "Only org admins or managers can modify relationships")
        yield agent


async def eligible_relationship_member(item: RelationshipIn, agent: AgentRecord) -> UUID:
    """Resolve and freshly validate a target under the caller's tenant/agent locks."""
    try:
        target_id = UUID(item.member_id.removeprefix("platform-user:"))
    except ValueError:
        raise HTTPException(400, "Invalid relationship member ID") from None
    if item.member_id.startswith("platform-user:"):
        user = await user_dao.get_with_identity(target_id, fresh=True)
        if user is None or user.tenant_id != agent.tenant_id or not user.is_active:
            raise HTTPException(400, "Platform user is not available")
        if await compute_agent_access_level(user, agent) is None:
            raise HTTPException(403, "Platform user does not have access to this agent")
        if agent.tenant_id is None:
            raise HTTPException(400, "Agent has no organization")
        member = await org_member_dao.get_active_by_user_and_tenant(user.id, agent.tenant_id)
        if member is None:
            member = await org_member_dao.create(
                obj_in={
                    "tenant_id": agent.tenant_id,
                    "user_id": user.id,
                    "external_id": f"platform:{user.id}",
                    "name": user.display_name or user.email or "User",
                    "email": user.email,
                    "avatar_url": user.avatar_url,
                    "title": user.title or "",
                    "status": "active",
                }
            )
    else:
        member = await org_member_dao.get(target_id)
    if member is None or member.tenant_id != agent.tenant_id or member.status != "active":
        raise HTTPException(400, "Relationship member is not available")
    if member.user_id is not None:
        user = await user_dao.get_with_identity(member.user_id, fresh=True)
        if (
            user is None
            or not user.is_active
            or user.tenant_id != agent.tenant_id
            or (user.identity is not None and not user.identity.is_active)
        ):
            raise HTTPException(400, "Relationship member is linked to an unavailable platform user")
        if await compute_agent_access_level(user, agent) is None:
            raise HTTPException(403, "Platform user does not have access to this agent")
    return member.id
