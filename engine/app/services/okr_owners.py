"""Tenant-scoped objective owner resolution."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.dao.agent_dao import agent_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.user_dao import user_dao
from app.records.okr import OKRObjectiveRecord


async def resolve_owner(tenant_id: UUID, owner_type: str, owner_id: UUID) -> UUID | None:
    if owner_type == "agent":
        agent = await agent_dao.get(owner_id)
        return owner_id if agent and agent.tenant_id == tenant_id else None
    if owner_type == "user":
        user = await user_dao.get(owner_id)
        if user:
            return owner_id if user.tenant_id == tenant_id else None
        member = await org_member_dao.get(owner_id)
        if not member or member.tenant_id != tenant_id:
            return None
        if member.user_id:
            user = await user_dao.get(member.user_id)
            return member.user_id if user and user.tenant_id == tenant_id else None
        return owner_id
    return None


async def owner_names(tenant_id: UUID, objectives: Sequence[OKRObjectiveRecord]) -> dict[tuple[str, UUID], str]:
    user_ids = {o.owner_id for o in objectives if o.owner_type == "user" and o.owner_id}
    agent_ids = {o.owner_id for o in objectives if o.owner_type == "agent" and o.owner_id}
    users = await user_dao.get_many_with_identity(list(user_ids)) if user_ids else {}
    names = {
        user.id: user.display_name or user.email or "User" for user in users.values() if user.tenant_id == tenant_id
    }
    missing = user_ids - users.keys()
    members = await org_member_dao.get_many(list(missing)) if missing else []
    linked_ids = [member.user_id for member in members if member.tenant_id == tenant_id and member.user_id]
    linked = await user_dao.get_many(linked_ids) if linked_ids else []
    valid_linked = {user.id for user in linked if user.tenant_id == tenant_id}
    names.update(
        {
            member.id: member.name
            for member in members
            if member.tenant_id == tenant_id and (not member.user_id or member.user_id in valid_linked)
        }
    )
    agents = await agent_dao.get_many(list(agent_ids)) if agent_ids else []
    user_names = {("user", owner_id): name for owner_id, name in names.items()}
    user_names.update({("agent", agent.id): agent.name for agent in agents if agent.tenant_id == tenant_id})
    return user_names
