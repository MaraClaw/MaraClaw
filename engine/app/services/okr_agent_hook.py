"""Hook to automatically bind new users and company-visible agents to the OKR Agent."""

import uuid
from typing import Any

from app.core.logging import logger
from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_dao import agent_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.org_member_dao import org_member_dao
from app.records.agent import AgentRecord


async def hook_new_org_member(db: Any, member_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """When a new OrgMember is created or bound, bind them to the system OKR Agent if it exists."""
    okr_agent = await _get_okr_agent(tenant_id)
    if not okr_agent:
        return

    existing = await agent_relationship_dao.get_for_agent_and_member(okr_agent.id, member_id)
    if not existing:
        await agent_relationship_dao.create(
            obj_in={
                "agent_id": okr_agent.id,
                "member_id": member_id,
                "relation": "okr_coordinator",
                "description": "",
            }
        )
        logger.info(f"[OKR Hook] Auto-bound OrgMember {member_id} to OKR Agent {okr_agent.id}")


async def sync_okr_agent_platform_members(db: Any, tenant_id: uuid.UUID) -> int:
    """Bind all existing active platform users in a tenant to its OKR Agent.

    hook_new_org_member covers newly-created or newly-bound members. This
    startup/backfill path covers users who already existed before OKR was
    enabled or before the hook was introduced.
    """
    okr_agent = await _get_okr_agent(tenant_id)
    if not okr_agent:
        return 0

    existing_member_ids = await agent_relationship_dao.list_member_ids_for_agent(okr_agent.id)
    members = await org_member_dao.list_active_with_user_for_tenant(tenant_id)
    added = 0
    for member in members:
        if member.id in existing_member_ids:
            continue
        await agent_relationship_dao.create(
            obj_in={
                "agent_id": okr_agent.id,
                "member_id": member.id,
                "relation": "okr_coordinator",
                "description": "",
            }
        )
        existing_member_ids.add(member.id)
        added += 1

    if added:
        logger.info(f"[OKR Hook] Backfilled {added} platform member(s) to OKR Agent {okr_agent.id}")

    return added


async def hook_new_agent(db: Any, new_agent_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """When a new company-visible agent is created, bind to OKR Agent."""
    agent = await agent_dao.get(new_agent_id)
    if not agent or getattr(agent, "is_system", False):
        return
    if (getattr(agent, "access_mode", None) or "company") != "company":
        return  # Do not bind private/custom agents into tenant-wide OKR relationships

    okr_agent = await _get_okr_agent(tenant_id)
    if not okr_agent:
        return

    await agent_agent_relationship_dao.ensure(okr_agent.id, new_agent_id, relation="okr_coordinator")
    await agent_agent_relationship_dao.ensure(new_agent_id, okr_agent.id, relation="okr_coordinator")

    logger.info(f"[OKR Hook] Auto-bound Agent {new_agent_id} to OKR Agent {okr_agent.id}")


async def _get_okr_agent(tenant_id: uuid.UUID) -> AgentRecord | None:
    # Find system agent named 'OKR Agent' in this tenant
    return await agent_dao.get_system_by_name(tenant_id, "OKR Agent")
