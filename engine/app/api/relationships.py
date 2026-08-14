"""Agent relationship management API - human + agent-to-agent."""

import uuid
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.permissions import (
    check_agent_access,
    evaluate_agent_relationship_status,
    evaluate_human_relationship_status,
    get_agent_access_level_for_user_id,
    get_agent_accessible_user_ids,
    list_visible_agents,
)
from app.core.security import get_current_user
from app.dao import agent_agent_relationship_dao, agent_dao, agent_relationship_dao, org_member_dao, user_dao
from app.records.agent import AgentRecord
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord
from app.services.access_relationships import ensure_access_granted_platform_relationships
from app.services.org_sync_adapter import derive_member_department_paths

router = APIRouter(prefix="/agents/{agent_id}/relationships", tags=["relationships"])

RELATION_LABELS = {
    "direct_leader": "Direct Leader",
    "collaborator": "Collaborator",
    "stakeholder": "Stakeholder",
    "team_member": "Team Member",
    "subordinate": "Subordinate",
    "mentor": "Mentor",
    "other": "Other",
}

AGENT_RELATION_LABELS = {
    "peer": "Peer Collaborator",
    "supervisor": "Supervisor Agent",
    "assistant": "Assistant",
    "collaborator": "Collaborator",
    "other": "Other",
}


def _can_manage_relationships(current_user: UserRecord, access_level: str) -> bool:
    return access_level == "manage" or current_user.role in ("platform_admin", "org_admin")


def _display_provider_name(provider_name: str | None, provider_type: str | None) -> str | None:
    if not provider_name and not provider_type:
        return None
    if (provider_type or "").lower() in ("web", "platform") or (provider_name or "").lower() == "web":
        return "Platform"
    return provider_name


async def _can_manage_agent(user_id: uuid.UUID, agent: AgentRecord) -> bool:
    return (await get_agent_access_level_for_user_id(None, user_id, agent)) == "manage"


async def _get_valid_member_user_id(
    member: OrgMemberRecord,
    tenant_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Return the linked platform user only when it belongs to the same tenant."""
    if member.user_id is None:
        return None
    user = await user_dao.get(member.user_id)
    if not user or not user.is_active or user.tenant_id != tenant_id:
        return None
    return user.id


# ─── Schemas ───────────────────────────────────────────


class RelationshipIn(BaseModel):
    member_id: str
    relation: str = "collaborator"
    description: str = ""


class RelationshipBatchIn(BaseModel):
    relationships: list[RelationshipIn]


class HumanRelationshipCandidate(TypedDict):
    id: str
    name: str | None
    email: str | None
    title: str | None
    department_path: str | None
    avatar_url: str | None
    external_id: str | None
    provider_id: str | None
    provider_name: str | None
    provider_type: str | None
    user_id: str | None
    is_platform_user: bool
    platform_access_level: str | None


class AgentRelationshipIn(BaseModel):
    target_agent_id: str
    relation: str = "collaborator"
    description: str = ""


class AgentRelationshipBatchIn(BaseModel):
    relationships: list[AgentRelationshipIn]


type HumanCandidateRow = tuple[OrgMemberRecord, str | None, str | None, uuid.UUID | None]


def _dedupe_human_relationships(items: list[RelationshipIn]) -> list[RelationshipIn]:
    deduped: dict[str, RelationshipIn] = {}
    for item in items:
        deduped[item.member_id] = item
    return list(deduped.values())


def _dedupe_agent_relationships(items: list[AgentRelationshipIn], agent_id: uuid.UUID) -> list[AgentRelationshipIn]:
    deduped: dict[str, AgentRelationshipIn] = {}
    for item in items:
        if item.target_agent_id == str(agent_id):
            continue
        deduped[item.target_agent_id] = item
    return list(deduped.values())


# ─── Human Relationships (existing) ───────────────────


@router.get("/")
async def get_relationships(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> list[dict[str, object]]:
    """Get all human relationships for this agent."""
    source_agent, _access_level = await check_agent_access(current_user, agent_id)
    if await ensure_access_granted_platform_relationships(
        None,
        source_agent,
        created_by_user_id=current_user.id,
    ):
        await _regenerate_relationships_file(agent_id)

    rows = await agent_relationship_dao.list_for_agent_with_members_and_providers(agent_id)
    member_paths = await derive_member_department_paths(
        None,
        [r.member for r in rows if r.member],
    )
    out: list[dict[str, object]] = []
    for r in rows:
        linked_user_id = await _get_valid_member_user_id(r.member, source_agent.tenant_id) if r.member else None
        out.append(
            {
                "id": str(r.id),
                "member_id": str(r.member_id),
                "relation": r.relation,
                "relation_label": RELATION_LABELS.get(r.relation, r.relation),
                "description": r.description,
                **(await evaluate_human_relationship_status(None, r, source_agent=source_agent)),
                "member": {
                    "name": r.member.name,
                    "title": r.member.title,
                    "department_path": member_paths.get(r.member.id, r.member.department_path),
                    "avatar_url": r.member.avatar_url,
                    "email": r.member.email,
                    "provider_name": _display_provider_name(r.provider_name, r.provider_type),
                    "provider_type": "platform" if (r.provider_type or "").lower() == "web" else r.provider_type,
                    "user_id": str(linked_user_id) if linked_user_id else None,
                    "is_platform_user": bool(linked_user_id),
                }
                if r.member
                else None,
            }
        )
    return out


@router.get("/member-candidates")
async def search_human_relationship_candidates(
    agent_id: uuid.UUID, search: str | None = None, current_user: UserRecord = Depends(get_current_user)
):
    """Search org members that are eligible for this agent's human relationships."""
    agent, access_level = await check_agent_access(current_user, agent_id)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")

    search_text = (search or "").strip()
    access_mode = agent.access_mode or "company"

    allowed_user_ids: list[uuid.UUID] | None = None
    if access_mode != "company":
        allowed_user_ids = list(await get_agent_accessible_user_ids(None, agent))

    if agent.tenant_id is None:
        return []
    rows = await org_member_dao.list_relationship_candidates(
        tenant_id=agent.tenant_id,
        search=search_text or None,
        allowed_user_ids=allowed_user_ids,
        limit=200,
    )

    deduped_filtered: list[HumanCandidateRow] = []
    by_user_id: dict[uuid.UUID, HumanCandidateRow] = {}
    for member, provider_name, provider_type, linked_user_id in rows:
        row = (member, provider_name, provider_type, linked_user_id)
        if not linked_user_id:
            deduped_filtered.append(row)
            continue
        existing = by_user_id.get(linked_user_id)
        if not existing:
            by_user_id[linked_user_id] = row
            continue
        existing_type = (existing[2] or "").lower()
        current_type = (provider_type or "").lower()
        if existing_type in ("", "web", "platform") and current_type not in ("", "web", "platform"):
            by_user_id[linked_user_id] = row
    filtered = [*deduped_filtered, *by_user_id.values()]

    filtered = sorted(filtered, key=lambda row: (row[0].name or "").lower())[:100]
    member_paths = await derive_member_department_paths(
        None,
        [m for m, _provider_name, _provider_type, _linked_user_id in filtered],
    )
    org_member_candidates: list[HumanRelationshipCandidate] = [
        {
            "id": str(m.id),
            "name": m.name,
            "email": m.email,
            "title": m.title,
            "department_path": member_paths.get(m.id, m.department_path),
            "avatar_url": m.avatar_url,
            "external_id": m.external_id,
            "provider_id": str(m.provider_id) if m.provider_id else None,
            "provider_name": _display_provider_name(provider_name, provider_type) if m.provider_id else None,
            "provider_type": "platform"
            if (provider_type or "").lower() == "web"
            else provider_type
            if m.provider_id
            else None,
            "user_id": str(linked_user_id) if linked_user_id else None,
            "is_platform_user": bool(linked_user_id),
            "platform_access_level": (
                await get_agent_access_level_for_user_id(None, linked_user_id, agent) if linked_user_id else None
            ),
        }
        for m, provider_name, provider_type, linked_user_id in filtered
    ]
    return sorted(org_member_candidates, key=lambda candidate: (candidate["name"] or "").lower())[:100]


@router.put("/")
async def save_relationships(
    agent_id: uuid.UUID, data: RelationshipBatchIn, current_user: UserRecord = Depends(get_current_user)
):
    """Replace all human relationships for this agent."""
    _agent, access_level = await check_agent_access(current_user, agent_id)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")

    existing_list = await agent_relationship_dao.list_for_agent(agent_id)
    existing_by_member = {r.member_id: r for r in existing_list}

    _ = await agent_relationship_dao.delete_for_agent(agent_id)

    for r in _dedupe_human_relationships(data.relationships):
        if r.member_id.startswith("platform-user:"):
            platform_user_id = uuid.UUID(r.member_id.split(":", 1)[1])
            platform_user = await user_dao.get_with_identity(platform_user_id)
            if not platform_user or platform_user.tenant_id != _agent.tenant_id or not platform_user.is_active:
                raise HTTPException(status_code=400, detail="Platform user is not available")
            if not await get_agent_access_level_for_user_id(None, platform_user.id, _agent):
                raise HTTPException(status_code=403, detail="Platform user does not have access to this agent")
            if _agent.tenant_id is None:
                raise HTTPException(status_code=400, detail="Agent has no organization")
            member = await org_member_dao.get_active_by_user_and_tenant(platform_user.id, _agent.tenant_id)
            if not member:
                member = await org_member_dao.create(
                    obj_in={
                        "tenant_id": _agent.tenant_id,
                        "user_id": platform_user.id,
                        "external_id": f"platform:{platform_user.id}",
                        "name": platform_user.display_name
                        or platform_user.username
                        or platform_user.email
                        or str(platform_user.id),
                        "email": platform_user.email,
                        "avatar_url": platform_user.avatar_url,
                        "title": platform_user.title or "",
                        "department_path": "",
                        "status": "active",
                    }
                )
            member_id = member.id
        else:
            member_id = uuid.UUID(r.member_id)
            member = await org_member_dao.get(member_id)
        if not member or member.tenant_id != _agent.tenant_id or member.status != "active":
            raise HTTPException(status_code=400, detail="Relationship member is not available")
        linked_user_id = await _get_valid_member_user_id(member, _agent.tenant_id)
        if member.user_id and not linked_user_id:
            raise HTTPException(status_code=400, detail="Relationship member is linked to an unavailable platform user")
        if linked_user_id and not await get_agent_access_level_for_user_id(None, linked_user_id, _agent):
            raise HTTPException(status_code=403, detail="Platform user does not have access to this agent")
        existing = existing_by_member.get(member_id)
        _ = await agent_relationship_dao.create(
            obj_in={
                "agent_id": agent_id,
                "member_id": member_id,
                "relation": r.relation,
                "description": r.description,
                "created_by_user_id": (existing.created_by_user_id if existing else None) or current_user.id,
                "updated_by_user_id": current_user.id,
            }
        )

    await _regenerate_relationships_file(agent_id)
    return {"status": "ok"}


@router.delete("/{rel_id}")
async def delete_relationship(
    agent_id: uuid.UUID, rel_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
):
    """Delete a single human relationship."""
    _agent, access_level = await check_agent_access(current_user, agent_id)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")
    rel = await agent_relationship_dao.get_for_agent_by_id(agent_id, rel_id)
    if rel:
        _ = await agent_relationship_dao.delete(id=rel_id)
        await _regenerate_relationships_file(agent_id)

    return {"status": "ok"}


# ─── Agent-to-Agent Relationships (new) ───────────────


@router.get("/agent-candidates")
async def search_visible_agents(
    agent_id: uuid.UUID, search: str | None = None, current_user: UserRecord = Depends(get_current_user)
) -> list[dict[str, object]]:
    """Search manageable agent candidates for relationship creation."""
    source_agent, access_level = await check_agent_access(current_user, agent_id)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")

    candidates = await list_visible_agents(
        current_user,
        tenant_id=source_agent.tenant_id,
        exclude_agent_id=agent_id,
        search=search,
        limit=50,
    )
    agents = [agent for agent in candidates if await _can_manage_agent(current_user.id, agent)]
    return [
        {
            "id": str(agent.id),
            "name": agent.name,
            "role_description": agent.role_description or "",
            "avatar_url": agent.avatar_url or "",
            "creator_id": str(agent.creator_id),
            "access_mode": agent.access_mode or "company",
            "can_manage": True,
        }
        for agent in agents
    ]


@router.get("/agents")
async def get_agent_relationships(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> list[dict[str, object]]:
    """Get all agent-to-agent relationships."""
    _ = await check_agent_access(current_user, agent_id)
    rels = await agent_agent_relationship_dao.list_for_agent_with_targets(agent_id)
    out: list[dict[str, object]] = []
    for r in rels:
        status_info = await evaluate_agent_relationship_status(None, r, current_user_id=current_user.id)
        out.append(
            {
                "id": str(r.id),
                "target_agent_id": str(r.target_agent_id),
                "relation": r.relation,
                "relation_label": AGENT_RELATION_LABELS.get(r.relation, r.relation),
                "description": r.description,
                **status_info,
                "target_agent": {
                    "id": str(r.target_agent.id),
                    "name": r.target_agent.name,
                    "role_description": r.target_agent.role_description or "",
                    "avatar_url": r.target_agent.avatar_url or "",
                    "access_mode": r.target_agent.access_mode or "company",
                }
                if r.target_agent
                else None,
            }
        )
    return out


@router.get("/agents/candidates")
async def get_agent_relationship_candidates(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> list[dict[str, object]]:
    """Backward-compatible alias for searchable agent candidates."""
    return await search_visible_agents(
        agent_id=agent_id,
        search=None,
        current_user=current_user,
    )


@router.put("/agents")
async def save_agent_relationships(
    agent_id: uuid.UUID, data: AgentRelationshipBatchIn, current_user: UserRecord = Depends(get_current_user)
):
    """Replace all agent-to-agent relationships."""
    source_agent, access_level = await check_agent_access(current_user, agent_id)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")

    existing_list = await agent_agent_relationship_dao.list_for_agent(agent_id)
    existing_by_target = {r.target_agent_id: r for r in existing_list}

    _ = await agent_agent_relationship_dao.delete_for_agent(agent_id)

    visible_ids = {
        a.id
        for a in await list_visible_agents(
            current_user,
            tenant_id=source_agent.tenant_id,
        )
    }

    for r in _dedupe_agent_relationships(data.relationships, agent_id):
        target_id = uuid.UUID(r.target_agent_id)
        if target_id not in visible_ids:
            raise HTTPException(status_code=403, detail="Target agent is not visible to the current user")
        target_agent = await agent_dao.get(target_id)
        if not target_agent:
            raise HTTPException(status_code=403, detail="Target agent is not visible to the current user")
        if not await _can_manage_agent(current_user.id, target_agent):
            raise HTTPException(status_code=403, detail="You must manage both agents to create this relationship")
        existing = existing_by_target.get(target_id)
        _ = await agent_agent_relationship_dao.create(
            obj_in={
                "agent_id": agent_id,
                "target_agent_id": target_id,
                "relation": r.relation,
                "description": r.description,
                "created_by_user_id": (existing.created_by_user_id if existing else None) or current_user.id,
                "updated_by_user_id": current_user.id,
            }
        )

    await _regenerate_relationships_file(agent_id)
    return {"status": "ok"}


@router.delete("/agents/{rel_id}")
async def delete_agent_relationship(
    agent_id: uuid.UUID, rel_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
):
    """Delete a single agent-to-agent relationship."""
    _agent, access_level = await check_agent_access(current_user, agent_id)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")
    rel = await agent_agent_relationship_dao.get_for_agent_by_id(agent_id, rel_id)
    if rel:
        _ = await agent_agent_relationship_dao.delete(id=rel_id)
        await _regenerate_relationships_file(agent_id)

    return {"status": "ok"}


# ─── relationships.md Generation ──────────────────────


async def _regenerate_relationships_file(agent_id: uuid.UUID):
    """Obsolete. relationships.md is no longer generated as relationships are read directly from the database."""
    _ = agent_id
