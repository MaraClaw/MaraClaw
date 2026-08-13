"""Helpers that keep access permissions and relationship prerequisites aligned."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.permissions import get_agent_accessible_user_ids
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.user_dao import user_dao
from app.services.registration_service import registration_service


async def ensure_access_granted_platform_relationships(
    db: Any,
    agent: Any,
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

    user_ids = await get_agent_accessible_user_ids(None, agent)
    if not user_ids:
        return False

    existing_user_ids = await agent_relationship_dao.list_related_user_ids(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        user_ids=list(user_ids),
    )
    missing_user_ids = user_ids - existing_user_ids
    if not missing_user_ids:
        return False

    changed = False
    for user_id in missing_user_ids:
        user = await user_dao.get_with_identity(user_id)
        if not user or not user.is_active or user.tenant_id != agent.tenant_id:
            continue
        member = await registration_service.ensure_web_org_member(user)
        if not member or member.status != "active":
            continue
        await agent_relationship_dao.create(
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
