"""Helpers that keep access permissions and relationship prerequisites aligned."""

from __future__ import annotations

import uuid

from app.core.acl_locks import lock_agent, lock_tenants
from app.core.permissions import get_agent_accessible_user_ids
from app.dao.agent_dao import agent_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.services.registration_service import registration_service


async def ensure_access_granted_platform_relationships(
    agent: AgentRecord,
    *,
    created_by_user_id: uuid.UUID | None = None,
) -> bool:
    """Ensure private/custom platform users are in the agent's human network.

    Platform messages intentionally require an active human relationship. For
    private/custom agents, the access list is already the user's explicit
    relationship boundary, so we materialize those platform users as human
    relationships. Company-wide agents stay explicit to avoid adding every
    tenant user to every public agent.

    Returns True when new relationship rows were added.
    """
    access_mode = getattr(agent, "access_mode", None) or "company"
    if access_mode not in ("private", "custom") or not agent.tenant_id:
        return False

    async with connection_ctx() as db:
        await lock_tenants(db, (agent.tenant_id,))
        await lock_agent(db, agent.id, agent.tenant_id)
        fresh = await agent_dao.get(agent.id)
        if fresh is None or (fresh.access_mode or "company") not in ("private", "custom") or fresh.tenant_id is None:
            return False
        return await _ensure_locked_relationships(fresh, created_by_user_id, fresh.tenant_id)


async def _ensure_locked_relationships(
    agent: AgentRecord, created_by_user_id: uuid.UUID | None, tenant_id: uuid.UUID
) -> bool:
    user_ids = await get_agent_accessible_user_ids(agent)
    if not user_ids:
        return False

    existing_user_ids = await agent_relationship_dao.list_related_user_ids(
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_ids=list(user_ids),
    )
    missing_user_ids = user_ids - existing_user_ids
    if not missing_user_ids:
        return False

    changed = False
    for user_id in sorted(missing_user_ids):
        user = await user_dao.get_with_identity(user_id, fresh=True)
        if (
            not user
            or not user.is_active
            or user.tenant_id != agent.tenant_id
            or (user.identity is not None and not user.identity.is_active)
        ):
            continue
        member = await registration_service.ensure_web_org_member(user)
        if not member or member.status != "active":
            continue
        if await agent_relationship_dao.get_for_agent_and_member(agent.id, member.id) is not None:
            changed = True
            continue
        _ = await agent_relationship_dao.create(
            obj_in={
                "agent_id": agent.id,
                "member_id": member.id,
                "relation": "collaborator",
                "description": "Auto-added from agent access permissions.",
                "created_by_user_id": created_by_user_id or agent.creator_id,
                "updated_by_user_id": created_by_user_id or agent.creator_id,
            }
        )
        changed = True

    return changed
