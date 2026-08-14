"""RBAC permission checking utilities (DAO / psycopg backed for access checks)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar, Protocol, TypedDict

from fastapi import HTTPException, status

from app.core import access_cache
from app.dao import agent_dao, agent_permission_dao, org_member_dao, user_dao
from app.records.agent import AgentPermissionRecord, AgentRecord
from app.records.agent_agent_relationship import AgentAgentRelationshipRecord
from app.records.agent_relationship import AgentRelationshipRecord


class _NeedPerms:
    __slots__: ClassVar[tuple[str, ...]] = ()


_NEED_PERMS = _NeedPerms()


class RelationshipStatus(TypedDict):
    access_allowed: bool
    access_status: str
    access_status_reason: str | None


class _UserLike(Protocol):
    id: uuid.UUID
    role: str
    tenant_id: uuid.UUID | None


class _AgentLike(Protocol):
    id: uuid.UUID
    creator_id: uuid.UUID
    tenant_id: uuid.UUID | None
    access_mode: str
    company_access_level: str
    status: str
    is_expired: bool
    expires_at: datetime | None


async def list_visible_agents(
    user: _UserLike,
    *,
    tenant_id: uuid.UUID | None = None,
    exclude_agent_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int | None = None,
) -> list[AgentRecord]:
    """Return visible agents using the pure-psycopg agent DAO."""
    target_tenant_id = tenant_id if tenant_id is not None else user.tenant_id
    return list(
        await agent_dao.list_visible_for_user(
            user_id=user.id,
            tenant_id=target_tenant_id,
            role=user.role,
            exclude_agent_id=exclude_agent_id,
            search=search,
            limit=limit,
        )
    )


def is_company_visible_agent(agent: _AgentLike) -> bool:
    """Return whether an agent participates in company-public surfaces."""
    return (getattr(agent, "access_mode", None) or "company") == "company"


def _is_admin(user: _UserLike) -> bool:
    return user.role in ("platform_admin", "org_admin")


def decide_agent_access(
    user: _UserLike,
    agent: _AgentLike,
    permissions: Sequence[AgentPermissionRecord] | None = None,
) -> str | None:
    """Return 'manage', 'use', or None. Does not load rows.

    When ``permissions`` is omitted and the decision needs permission rows,
    treat the list as empty (caller should load first).
    """
    early = _access_without_permissions(user, agent)
    if isinstance(early, _NeedPerms):
        return _access_from_permissions(user, agent, permissions or ())
    return early


def _access_without_permissions(user: _UserLike, agent: _AgentLike) -> str | None | _NeedPerms:
    if getattr(agent, "tenant_id", None) != getattr(user, "tenant_id", None):
        return None
    if getattr(agent, "creator_id", None) == getattr(user, "id", None):
        return "manage"
    access_mode = getattr(agent, "access_mode", None) or "company"
    if _is_admin(user) and access_mode != "private":
        return "manage"
    company_level = getattr(agent, "company_access_level", None)
    if access_mode == "company" and company_level:
        return company_level
    return _NEED_PERMS


def _access_from_permissions(
    user: _UserLike, agent: _AgentLike, permissions: Sequence[AgentPermissionRecord]
) -> str | None:
    access_mode = getattr(agent, "access_mode", None) or "company"
    if access_mode == "company":
        company_level = getattr(agent, "company_access_level", None) or next(
            (perm.access_level for perm in permissions if perm.scope_type == "company"),
            "use",
        )
        return company_level or "use"
    if access_mode == "custom":
        for perm in permissions:
            if perm.scope_type == "user" and perm.scope_id == user.id:
                return perm.access_level or "use"
    return None


async def _compute_access_level(user: _UserLike, agent: _AgentLike) -> str | None:
    early = _access_without_permissions(user, agent)
    if isinstance(early, _NeedPerms):
        permissions = await agent_permission_dao.list_for_agent(agent.id)
        return _access_from_permissions(user, agent, permissions)
    return early


async def get_agent_access_level_for_user_id(
    db: object | None,
    user_id: uuid.UUID | None,
    agent: _AgentLike,
) -> str | None:
    """Return 'manage', 'use', or None for a platform user and an agent.

    ``db`` is accepted for call-site compatibility and ignored.
    """
    del db
    if not user_id:
        return None

    memo = access_cache.memo_get(user_id, agent.id)
    if memo is not None:
        return memo[1]

    user = await user_dao.get(user_id)
    if not user or not user.is_active:
        return None

    cached = await access_cache.get_cached_level(user, agent.id)
    if cached is not None:
        return cached

    level = await _compute_access_level(user, agent)
    if level in {"manage", "use"}:
        await access_cache.set_cached_level(user, agent.id, level)
    return level


async def user_can_manage_agent_id(
    db: object | None,
    user_id: uuid.UUID | None,
    agent: _AgentLike,
) -> bool:
    return (await get_agent_access_level_for_user_id(db, user_id, agent)) == "manage"


async def get_agent_accessible_user_ids(db: object | None, agent: AgentRecord) -> set[uuid.UUID]:
    """Return platform users who can access an agent under current policy."""
    del db
    access_mode = getattr(agent, "access_mode", None) or "company"
    ids: set[uuid.UUID] = set()
    if agent.creator_id:
        ids.add(agent.creator_id)

    if access_mode == "company":
        if agent.tenant_id is not None:
            ids.update(await user_dao.list_active_ids_for_tenant(agent.tenant_id))
        return ids

    if access_mode == "custom":
        ids.update(await agent_permission_dao.list_user_scope_ids(agent.id))
        if agent.tenant_id is not None:
            ids.update(await user_dao.list_active_admin_ids_for_tenant(agent.tenant_id))

    return ids


def _agent_available(agent: AgentRecord | None) -> tuple[bool, str | None]:
    if not agent:
        return False, "target_not_found"
    if getattr(agent, "status", None) in ("stopped", "error"):
        return False, f"target_status_{agent.status}"
    if is_agent_expired(agent):
        return False, "target_expired"
    return True, None


async def evaluate_agent_relationship_status(
    db: object | None,
    rel: AgentAgentRelationshipRecord,
    *,
    current_user_id: uuid.UUID | None = None,
) -> RelationshipStatus:
    """Compute the effective status for an Agent -> Agent relationship."""
    del db
    source = await agent_dao.get(rel.agent_id)
    target = getattr(rel, "target_agent", None)
    if target is None:
        target = await agent_dao.get(rel.target_agent_id)

    if not source or not target:
        return {
            "access_allowed": False,
            "access_status": "missing_target",
            "access_status_reason": "source_or_target_not_found",
        }
    if source.tenant_id != target.tenant_id:
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": "different_tenant",
        }

    available, reason = _agent_available(target)
    if not available:
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": reason or "target_unavailable",
        }

    created_by_user_id = getattr(rel, "created_by_user_id", None)
    if created_by_user_id:
        if await user_can_manage_agent_id(None, created_by_user_id, source) and await user_can_manage_agent_id(
            None, created_by_user_id, target
        ):
            return {
                "access_allowed": True,
                "access_status": "active",
                "access_status_reason": None,
            }
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": "relationship_creator_no_longer_manages_both_agents",
        }

    target_mode = getattr(target, "access_mode", None) or "company"
    if target_mode == "company":
        return {
            "access_allowed": True,
            "access_status": "active",
            "access_status_reason": None,
        }

    candidate_user_ids: list[uuid.UUID | None] = [
        current_user_id,
        source.creator_id,
    ]
    seen: set[uuid.UUID] = set()
    for user_id in candidate_user_ids:
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        if await user_can_manage_agent_id(None, user_id, source) and await user_can_manage_agent_id(
            None, user_id, target
        ):
            return {
                "access_allowed": True,
                "access_status": "active",
                "access_status_reason": None,
            }

    return {
        "access_allowed": False,
        "access_status": "restricted",
        "access_status_reason": "manager_no_longer_has_access_to_both_agents",
    }


async def evaluate_human_relationship_status(
    db: object | None,
    rel: AgentRelationshipRecord,
    *,
    source_agent: AgentRecord | None = None,
) -> RelationshipStatus:
    """Compute the effective status for an Agent -> Human relationship."""
    del db
    if source_agent is None:
        source_agent = await agent_dao.get(rel.agent_id)
    # Works for ORM instances (relationship attr) and plain records (no member attr).
    member = getattr(rel, "member", None)
    if member is None:
        member = await org_member_dao.get(rel.member_id)

    if not source_agent or not member:
        return {
            "access_allowed": False,
            "access_status": "missing_target",
            "access_status_reason": "agent_or_member_not_found",
        }
    if member.status != "active":
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": "member_inactive",
        }
    if member.tenant_id and source_agent.tenant_id and member.tenant_id != source_agent.tenant_id:
        return {
            "access_allowed": False,
            "access_status": "restricted",
            "access_status_reason": "different_tenant",
        }
    if member.user_id:
        access_level = await get_agent_access_level_for_user_id(None, member.user_id, source_agent)
        if not access_level:
            return {
                "access_allowed": False,
                "access_status": "restricted",
                "access_status_reason": "platform_user_no_agent_access",
            }

    return {
        "access_allowed": True,
        "access_status": "active",
        "access_status_reason": None,
    }


async def check_agent_access(
    user: _UserLike, agent_id: uuid.UUID, db: object | None = None
) -> tuple[AgentRecord, str]:
    """Check if a user has access to a specific agent.

    Returns (agent, access_level) where access_level is 'manage' or 'use'.
    ``db`` is optional and ignored (legacy dual-stack parameter).
    """
    del db
    user_id = getattr(user, "id", None)
    if user_id is not None:
        memo = access_cache.memo_get(user_id, agent_id)
        if memo is not None:
            return memo

    agent = await agent_dao.get(agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    cached = await access_cache.get_cached_level(user, agent_id)
    if cached is not None:
        if user_id is not None:
            access_cache.memo_set(user_id, agent_id, agent, cached)
        return agent, cached

    observed_ver = await access_cache.read_acl_version(agent_id)
    level = await _compute_access_level(user, agent)
    if level not in {"manage", "use"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this agent")

    await access_cache.set_cached_level(user, agent_id, level, observed_ver=observed_ver)
    if user_id is not None:
        access_cache.memo_set(user_id, agent_id, agent, level)
    return agent, level


def is_agent_creator(user: _UserLike, agent: _AgentLike) -> bool:
    """Check if the user is the creator (admin) of the agent."""
    return agent.creator_id == user.id


def is_agent_expired(agent: _AgentLike) -> bool:
    """Return True if the agent is manually marked expired or its expires_at is in the past."""
    expires_at = getattr(agent, "expires_at", None)
    return bool(getattr(agent, "is_expired", False) or (expires_at and datetime.now(UTC) > expires_at))
