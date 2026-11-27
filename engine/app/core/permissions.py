"""RBAC permission checking utilities (DAO / psycopg backed for access checks)."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Protocol, TypedDict

from fastapi import HTTPException, status

from app.core import access_cache
from app.core.domain.agents import (
    AgentAccessDecision,
    AgentAccessReason,
    AgentSharingMode,
    AgentStatus,
    CompanyAccessLevel,
    PermissionAccessLevel,
)
from app.core.domain.users import UserRole
from app.dao import agent_dao, agent_permission_dao, org_member_dao, user_dao
from app.dao.org_department_dao import org_department_dao
from app.db.session import optional_connection_ctx
from app.records.agent import AgentPermissionRecord, AgentRecord
from app.records.agent_agent_relationship import AgentAgentRelationshipRecord
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord
from app.schemas.agent_directory import AgentDirectoryPosition


class _NeedPerms:
    __slots__: ClassVar[tuple[str, ...]] = ()


_NEED_PERMS = _NeedPerms()


class RelationshipStatus(TypedDict):
    access_allowed: bool
    access_status: str
    access_status_reason: str | None


class _UserLike(Protocol):
    id: uuid.UUID
    role: UserRole
    tenant_id: uuid.UUID | None


class _AgentLike(Protocol):
    id: uuid.UUID
    creator_id: uuid.UUID
    tenant_id: uuid.UUID | None
    access_mode: AgentSharingMode
    company_access_level: CompanyAccessLevel
    status: AgentStatus
    is_expired: bool
    expires_at: datetime | None


type _UserAgentPair = tuple[uuid.UUID, uuid.UUID]


async def list_visible_agents(
    user: _UserLike,
    *,
    tenant_id: uuid.UUID | None = None,
    exclude_agent_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int | None = None,
    after: AgentDirectoryPosition | None = None,
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
            after=after,
        )
    )


def is_company_visible_agent(agent: _AgentLike) -> bool:
    """Return whether an agent participates in company-public surfaces."""
    return (agent.access_mode or "company") == "company"


def _is_admin(user: _UserLike) -> bool:
    return user.role in ("platform_admin", "org_admin")


def decide_agent_access(
    user: _UserLike,
    agent: _AgentLike,
    permissions: Sequence[AgentPermissionRecord] | None = None,
) -> PermissionAccessLevel | None:
    """Return 'manage', 'use', or None. Does not load rows.

    When ``permissions`` is omitted and the decision needs permission rows,
    treat the list as empty (caller should load first).
    """
    early = _access_without_permissions(user, agent)
    decision = _access_from_permissions(user, agent, permissions or ()) if isinstance(early, _NeedPerms) else early
    return decision.level if decision is not None else None


def _access_without_permissions(user: _UserLike, agent: _AgentLike) -> AgentAccessDecision | _NeedPerms | None:
    if agent.tenant_id != user.tenant_id:
        return None
    if agent.creator_id == user.id:
        return AgentAccessDecision(PermissionAccessLevel.MANAGE, AgentAccessReason.CREATOR)
    access_mode = agent.access_mode or "company"
    if _is_admin(user) and access_mode != "private":
        return AgentAccessDecision(PermissionAccessLevel.MANAGE, AgentAccessReason.ADMINISTRATOR)
    company_level = agent.company_access_level
    if access_mode == "company" and company_level:
        return AgentAccessDecision(company_level, AgentAccessReason.COMPANY)
    return _NEED_PERMS


def _access_from_permissions(
    user: _UserLike, agent: _AgentLike, permissions: Sequence[AgentPermissionRecord]
) -> AgentAccessDecision | None:
    access_mode = agent.access_mode or "company"
    if access_mode == "company":
        return AgentAccessDecision(
            agent.company_access_level
            or next(
                (perm.access_level for perm in permissions if perm.scope_type == "company"),
                PermissionAccessLevel.USE,
            ),
            AgentAccessReason.COMPANY,
        )
    if access_mode == "custom":
        for perm in permissions:
            if perm.scope_type == "user" and perm.scope_id == user.id:
                return AgentAccessDecision(perm.access_level, AgentAccessReason.DIRECT)
    return None


async def compute_agent_access_level(user: _UserLike, agent: _AgentLike) -> PermissionAccessLevel | None:
    decision = await compute_agent_access_decision(user, agent)
    return decision.level if decision is not None else None


async def compute_agent_access_decision(user: _UserLike, agent: _AgentLike) -> AgentAccessDecision | None:
    """Resolve the winning access level and viewer-specific provenance together."""
    early = _access_without_permissions(user, agent)
    if isinstance(early, _NeedPerms):
        permissions = await agent_permission_dao.list_for_agent(agent.id)
        direct = _access_from_permissions(user, agent, permissions)
        if direct is not None:
            return direct
        if agent.access_mode == "custom" and await org_department_dao.has_agent_access(user.id, agent.id):
            return AgentAccessDecision(PermissionAccessLevel.USE, AgentAccessReason.DEPARTMENT)
        return None
    return early


async def compute_agent_access_decisions(
    user: _UserLike, agents: Sequence[_AgentLike]
) -> dict[uuid.UUID, AgentAccessDecision | None]:
    """Batch form of ``compute_agent_access_decision`` with a fixed number of queries."""
    decisions: dict[uuid.UUID, AgentAccessDecision | None] = {}
    unresolved: dict[uuid.UUID, _AgentLike] = {}
    for agent in agents:
        early = _access_without_permissions(user, agent)
        if isinstance(early, _NeedPerms):
            unresolved[agent.id] = agent
        else:
            decisions[agent.id] = early
    if not unresolved:
        return decisions

    permissions = await agent_permission_dao.list_decision_rows(list(unresolved), user.id)
    department_candidates: list[uuid.UUID] = []
    for agent_id, agent in unresolved.items():
        direct = _access_from_permissions(user, agent, permissions.get(agent_id, ()))
        decisions[agent_id] = direct
        if direct is None and agent.access_mode == "custom":
            department_candidates.append(agent_id)
    if department_candidates:
        granted = await org_department_dao.accessible_agent_ids(user.id, department_candidates)
        for agent_id in granted:
            decisions[agent_id] = AgentAccessDecision(PermissionAccessLevel.USE, AgentAccessReason.DEPARTMENT)
    return decisions


async def get_agent_access_level_for_user_id(
    user_id: uuid.UUID | None,
    agent: _AgentLike,
) -> PermissionAccessLevel | None:
    """Return 'manage', 'use', or None for a platform user and an agent."""
    if not user_id:
        return None

    snapshot = await access_cache.read_acl_snapshot(user_id, agent.id)
    user = await user_dao.get(user_id)
    if not user or not user.is_active:
        return None

    cached = await access_cache.get_cached_level(user, agent.id, snapshot)
    if cached is not None:
        return cached

    from app.core.row_memo import memo_drop

    memo_drop("agent", agent.id)
    current_agent = await agent_dao.get(agent.id)
    if current_agent is None:
        return None
    level = await compute_agent_access_level(user, current_agent)
    if level is not None:
        await access_cache.set_cached_level(user, agent.id, level, observed_ver=snapshot.version)
    return level


async def user_can_manage_agent_id(
    user_id: uuid.UUID | None,
    agent: _AgentLike,
) -> bool:
    return (await get_agent_access_level_for_user_id(user_id, agent)) == "manage"


def _agent_tenant_id(agent: AgentRecord) -> uuid.UUID | None:
    return agent.tenant_id


async def get_agent_accessible_user_ids(agent: AgentRecord) -> set[uuid.UUID]:
    """Return platform users who can access an agent under current policy."""
    access_mode = agent.access_mode or "company"
    if agent.tenant_id is None:
        return set()
    active_ids = set(await user_dao.list_active_ids_for_tenant(agent.tenant_id))
    ids: set[uuid.UUID] = set()
    if agent.creator_id:
        ids.add(agent.creator_id)

    if access_mode == "company":
        if (tenant_id := _agent_tenant_id(agent)) is not None:
            ids.update(await user_dao.list_active_ids_for_tenant(tenant_id))
        return ids & active_ids

    if access_mode == "custom":
        ids.update(await agent_permission_dao.list_user_scope_ids(agent.id))
        ids.update(await org_department_dao.accessible_user_ids(agent.id))
        if (tenant_id := _agent_tenant_id(agent)) is not None:
            ids.update(await user_dao.list_active_admin_ids_for_tenant(tenant_id))

    return ids & active_ids


def _agent_available(agent: AgentRecord | None) -> tuple[bool, str | None]:
    if not agent:
        return False, "target_not_found"
    if agent.status in ("stopped", "error"):
        return False, f"target_status_{agent.status}"
    if is_agent_expired(agent):
        return False, "target_expired"
    return True, None


def _status(access_allowed: bool, access_status: str, reason: str | None = None) -> RelationshipStatus:
    return {"access_allowed": access_allowed, "access_status": access_status, "access_status_reason": reason}


def _active_status() -> RelationshipStatus:
    return _status(True, "active")


def _restricted_status(reason: str) -> RelationshipStatus:
    return _status(False, "restricted", reason)


@dataclass(frozen=True, slots=True)
class _ManagerGate:
    """An Agent -> Agent verdict that waits on whether one candidate user manages both agents."""

    source: AgentRecord
    target: AgentRecord
    candidate_user_ids: tuple[uuid.UUID, ...]
    denied_reason: str

    def status(self, *, managed: bool) -> RelationshipStatus:
        return _active_status() if managed else _restricted_status(self.denied_reason)


@dataclass(frozen=True, slots=True)
class _PlatformUserGate:
    """An Agent -> Human verdict that waits on the linked platform user's access level to the agent."""

    user_id: uuid.UUID
    agent: AgentRecord


def _decide_agent_relationship(
    source: AgentRecord | None,
    target: AgentRecord | None,
    *,
    created_by_user_id: uuid.UUID | None,
    current_user_id: uuid.UUID | None,
) -> RelationshipStatus | _ManagerGate:
    """Agent -> Agent precedence shared by the per-row and batch evaluators; loads nothing.

    Order: missing agent, different tenant, target availability, then the relationship creator (which never falls
    through to company mode), a company target, and finally the current user and the source agent's creator.
    A gate leaves only the access-level question, which each evaluator answers from its own loads.
    """
    if not source or not target:
        return _status(False, "missing_target", "source_or_target_not_found")
    if source.tenant_id != target.tenant_id:
        return _restricted_status("different_tenant")
    available, reason = _agent_available(target)
    if not available:
        return _restricted_status(reason or "target_unavailable")
    if created_by_user_id:
        return _ManagerGate(source, target, (created_by_user_id,), "relationship_creator_no_longer_manages_both_agents")
    if (target.access_mode or "company") == "company":
        return _active_status()
    candidates = tuple(dict.fromkeys(user_id for user_id in (current_user_id, source.creator_id) if user_id))
    return _ManagerGate(source, target, candidates, "manager_no_longer_has_access_to_both_agents")


def _decide_human_relationship(
    source_agent: AgentRecord | None, member: OrgMemberRecord | None
) -> RelationshipStatus | _PlatformUserGate:
    """Agent -> Human precedence shared by the per-row and batch evaluators; loads nothing."""
    if not source_agent or not member:
        return _status(False, "missing_target", "agent_or_member_not_found")
    if member.status != "active":
        return _restricted_status("member_inactive")
    if member.tenant_id and source_agent.tenant_id and member.tenant_id != source_agent.tenant_id:
        return _restricted_status("different_tenant")
    if member.user_id:
        return _PlatformUserGate(member.user_id, source_agent)
    return _active_status()


def _human_status_for_level(access_level: PermissionAccessLevel | None) -> RelationshipStatus:
    if access_level in (None, ""):
        return _restricted_status("platform_user_no_agent_access")
    return _active_status()


async def evaluate_agent_relationship_status(
    rel: AgentAgentRelationshipRecord,
    *,
    current_user_id: uuid.UUID | None = None,
) -> RelationshipStatus:
    """Compute the effective status for an Agent -> Agent relationship."""
    source = await agent_dao.get(rel.agent_id)
    target = rel.target_agent
    if target is None:
        target = await agent_dao.get(rel.target_agent_id)

    verdict = _decide_agent_relationship(
        source, target, created_by_user_id=rel.created_by_user_id, current_user_id=current_user_id
    )
    if not isinstance(verdict, _ManagerGate):
        return verdict
    for user_id in verdict.candidate_user_ids:
        if await user_can_manage_agent_id(user_id, verdict.source) and await user_can_manage_agent_id(
            user_id, verdict.target
        ):
            return verdict.status(managed=True)
    return verdict.status(managed=False)


async def evaluate_human_relationship_status(
    rel: AgentRelationshipRecord,
    *,
    source_agent: AgentRecord | None = None,
) -> RelationshipStatus:
    """Compute the effective status for an Agent -> Human relationship."""
    if source_agent is None:
        source_agent = await agent_dao.get(rel.agent_id)
    member = rel.member
    if member is None:
        member = await org_member_dao.get(rel.member_id)

    verdict = _decide_human_relationship(source_agent, member)
    if not isinstance(verdict, _PlatformUserGate):
        return verdict
    return _human_status_for_level(await get_agent_access_level_for_user_id(verdict.user_id, verdict.agent))


async def _fresh_agents(agent_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, AgentRecord]:
    """Read agent rows straight from SQL; ``agent_dao.get`` may serve the request memo."""
    return {agent.id: agent for agent in await agent_dao.get_many(list(agent_ids))}


async def _access_levels_for_pairs(
    pairs: Collection[_UserAgentPair], agents: Mapping[uuid.UUID, AgentRecord]
) -> dict[_UserAgentPair, PermissionAccessLevel | None]:
    """Batch form of ``get_agent_access_level_for_user_id`` in at most three statements.

    Users are read fresh by id, ``agents`` are the caller's fresh rows, and each decision runs through the same
    ``_access_without_permissions`` / ``_access_from_permissions`` as the per-row path. It neither reads nor fills
    the access cache, so no decision is served stale and no negative result is written.
    """
    if not pairs:
        return {}
    users = {user.id: user for user in await user_dao.get_many(list({user_id for user_id, _ in pairs}))}
    levels: dict[_UserAgentPair, PermissionAccessLevel | None] = {}
    undecided: list[tuple[_UserAgentPair, UserRecord, AgentRecord]] = []
    for pair in pairs:
        user, agent = users.get(pair[0]), agents.get(pair[1])
        if user is None or not user.is_active or agent is None:
            levels[pair] = None
            continue
        early = _access_without_permissions(user, agent)
        if isinstance(early, _NeedPerms):
            undecided.append((pair, user, agent))
        else:
            levels[pair] = early.level if early is not None else None
    if not undecided:
        return levels

    permissions = await agent_permission_dao.list_decision_rows_for_users(
        list({agent.id for _, _, agent in undecided}), list({user.id for _, user, _ in undecided})
    )
    departmental: list[_UserAgentPair] = []
    for pair, user, agent in undecided:
        direct = _access_from_permissions(user, agent, permissions.get(agent.id, ()))
        levels[pair] = direct.level if direct is not None else None
        if direct is None and agent.access_mode == "custom":
            departmental.append(pair)
    if departmental:
        granted = await org_department_dao.accessible_agent_pairs(
            list({user_id for user_id, _ in departmental}), list({agent_id for _, agent_id in departmental})
        )
        for pair in departmental:
            if pair in granted:
                levels[pair] = PermissionAccessLevel.USE
    return levels


async def evaluate_agent_relationship_statuses(
    rels: Sequence[AgentAgentRelationshipRecord],
    *,
    current_user_id: uuid.UUID | None = None,
) -> list[RelationshipStatus]:
    """Batch form of ``evaluate_agent_relationship_status``, aligned with ``rels``.

    Statements issued, whatever the number of relationships: one agent read, then (only when a verdict waits on a
    user) one user read, one permission read and one department read. Agents and users are read fresh; nothing is
    taken from the preloaded relationship rows or the access cache, and no row is locked.
    """
    if not rels:
        return []
    agents = await _fresh_agents({agent_id for rel in rels for agent_id in (rel.agent_id, rel.target_agent_id)})
    verdicts = [
        _decide_agent_relationship(
            agents.get(rel.agent_id),
            agents.get(rel.target_agent_id),
            created_by_user_id=rel.created_by_user_id,
            current_user_id=current_user_id,
        )
        for rel in rels
    ]
    levels = await _access_levels_for_pairs(
        {
            (user_id, agent.id)
            for verdict in verdicts
            if isinstance(verdict, _ManagerGate)
            for user_id in verdict.candidate_user_ids
            for agent in (verdict.source, verdict.target)
        },
        agents,
    )
    return [
        verdict.status(
            managed=any(
                levels[(user_id, verdict.source.id)] == "manage" and levels[(user_id, verdict.target.id)] == "manage"
                for user_id in verdict.candidate_user_ids
            )
        )
        if isinstance(verdict, _ManagerGate)
        else verdict
        for verdict in verdicts
    ]


async def evaluate_human_relationship_statuses(
    rels: Sequence[AgentRelationshipRecord],
) -> list[RelationshipStatus]:
    """Batch form of ``evaluate_human_relationship_status``, aligned with ``rels``.

    Statements issued, whatever the number of relationships: one agent read, one member read only for rows that
    arrived without their member, then (only when a verdict waits on a platform user) one user read, one permission
    read and one department read. Agents and users are read fresh, never from the request memo, the preloaded rows'
    agents or the access cache, and no row is locked.
    """
    if not rels:
        return []
    agents = await _fresh_agents({rel.agent_id for rel in rels})
    loaded_members = {
        member.id: member
        for member in await org_member_dao.get_many([rel.member_id for rel in rels if rel.member is None])
    }
    verdicts = [
        _decide_human_relationship(
            agents.get(rel.agent_id), rel.member if rel.member is not None else loaded_members.get(rel.member_id)
        )
        for rel in rels
    ]
    levels = await _access_levels_for_pairs(
        {(verdict.user_id, verdict.agent.id) for verdict in verdicts if isinstance(verdict, _PlatformUserGate)}, agents
    )
    return [
        _human_status_for_level(levels[(verdict.user_id, verdict.agent.id)])
        if isinstance(verdict, _PlatformUserGate)
        else verdict
        for verdict in verdicts
    ]


async def check_agent_access(
    user: _UserLike, agent_id: uuid.UUID, *, fresh: bool = False, user_is_current: bool = False
) -> tuple[AgentRecord, PermissionAccessLevel]:
    """Check if a user has access to a specific agent.

    Returns (agent, access_level) where access_level is 'manage' or 'use'. The transfer gate and every read share one
    connection. An agent decided by the caller's ``user`` and the agent alone (creator, administrator, company level,
    another tenant) never touches the decision cache or reloads the user. Otherwise the ACL version is observed first,
    then the user and the agent are read again, so a cached or filled decision never rests on older inputs. A ``fresh``
    check re-reads the user from SQL up front unless ``user_is_current`` says the caller just loaded it there.
    """
    if not getattr(user, "is_active", True):
        raise HTTPException(403, "User is inactive")
    if fresh:
        access_cache.clear_request_memo()
        from app.core.row_memo import clear_entity_memo

        clear_entity_memo()
    user_id = user.id
    from app.services.company_transfer_runtime_gate import assert_transfer_ready

    async with optional_connection_ctx():
        await assert_transfer_ready(agent_id)
        memo = access_cache.memo_get(user_id, agent_id)
        if memo is not None:
            _reject_expired_use(memo[0], memo[1])
            return memo

        if fresh and not user_is_current:
            user = await _read_current_user(user_id)
        agent = await _read_current_agent(agent_id)
        observed: str | None = None
        cached: PermissionAccessLevel | None = None
        cacheable = not fresh and isinstance(_access_without_permissions(user, agent), _NeedPerms)
        if cacheable:
            snapshot = await access_cache.read_acl_snapshot(user_id, agent_id)
            observed = snapshot.version
            user = await _read_current_user(user_id)
            cached = await access_cache.get_cached_level(user, agent_id, snapshot)
            if cached is None:
                agent = await _read_current_agent(agent_id)

        level = cached if cached is not None else await compute_agent_access_level(user, agent)
        if level is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this agent")
        _reject_expired_use(agent, level)

        if not fresh:
            if cacheable and cached is None:
                await access_cache.set_cached_level(user, agent_id, level, observed_ver=observed)
            access_cache.memo_set(user_id, agent_id, agent, level)
        return agent, level


async def _read_current_user(user_id: uuid.UUID) -> UserRecord:
    user = await user_dao.get(user_id)
    if user is None or not user.is_active:
        raise HTTPException(403, "User is inactive")
    return user


async def _read_current_agent(agent_id: uuid.UUID) -> AgentRecord:
    from app.core.row_memo import memo_drop

    memo_drop("agent", agent_id)
    agent = await agent_dao.get(agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


def is_agent_creator(user: _UserLike, agent: _AgentLike) -> bool:
    """Check if the user is the creator (admin) of the agent."""
    return agent.creator_id == user.id


def is_agent_expired(agent: _AgentLike) -> bool:
    """Return True if the agent is manually marked expired or its expires_at is in the past."""
    expires_at = getattr(agent, "expires_at", None)
    is_expired = bool(getattr(agent, "is_expired", False))
    return bool(is_expired or (expires_at and datetime.now(UTC) > expires_at))


def _reject_expired_use(agent: _AgentLike, level: PermissionAccessLevel) -> None:
    """Block use-level access to expired agents; manage can still extend them."""
    if level == "use" and is_agent_expired(agent):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This Agent has expired and is off duty. Please contact your admin to extend its service.",
        )
