"""Atomic replacement of the OKR agent's human and company-agent network."""

from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.security import raise_if_password_change_required
from app.dao import agent_agent_relationship_dao, agent_dao, agent_relationship_dao, org_member_dao, user_dao
from app.db.session import connection_ctx
from app.records.user import UserRecord


async def sync_okr_agent_relationships(tenant_id: UUID, okr_agent_id: UUID, actor: UserRecord) -> None:
    """Serialize replacement with all human-network writers and recheck eligibility."""
    async with connection_ctx() as db:
        await lock_tenants(db, (tenant_id, actor.tenant_id))
        await lock_user(db, actor.id, actor.tenant_id)
        user = await user_dao.get_with_identity(actor.id, fresh=True)
        if (
            user is None
            or not user.is_active
            or (user.identity is not None and not user.identity.is_active)
            or user.role not in ("org_admin", "platform_admin")
        ):
            raise HTTPException(403, "Only active org admins can sync OKR relationships")
        raise_if_password_change_required(user)
        await lock_agent(db, okr_agent_id, tenant_id)

        await agent_relationship_dao.delete_for_agent(okr_agent_id)
        await agent_agent_relationship_dao.delete_for_agent(okr_agent_id)

        for member_id in await org_member_dao.list_active_ids_for_tenant(tenant_id):
            member = await org_member_dao.get(member_id)
            if member is None or member.tenant_id != tenant_id or member.status != "active":
                continue
            if member.user_id is not None:
                linked_user = await user_dao.get_with_identity(member.user_id, fresh=True)
                if (
                    linked_user is None
                    or not linked_user.is_active
                    or linked_user.tenant_id != tenant_id
                    or (linked_user.identity is not None and not linked_user.identity.is_active)
                ):
                    continue
            await agent_relationship_dao.create(
                obj_in={
                    "agent_id": okr_agent_id,
                    "member_id": member.id,
                    "relation": "team_member",
                    "description": "OKR tracking - auto-linked via Sync Relationships",
                }
            )

        for agent in await agent_dao.list_active_nonsystem_for_tenant(
            tenant_id, exclude_id=okr_agent_id, company_only=True
        ):
            await agent_agent_relationship_dao.create(
                obj_in={"agent_id": okr_agent_id, "target_agent_id": agent.id, "relation": "collaborator"}
            )
