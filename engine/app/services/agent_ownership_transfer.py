"""Move agents between same-company owners without bypassing quotas, ACLs or pending deletions."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.domain.agents import PermissionAccessLevel, PermissionGrantScope
from app.core.notification_kinds import NotificationKind
from app.dao.agent_dao import agent_dao, agent_permission_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.agent_deletion_request import CancelReason
from app.records.user import UserRecord
from app.schemas.agent_ownership_transfer import OwnershipTransferResponse
from app.services.admin_audit import write_admin_audit
from app.services.agent_deletion_resolution import cancel_pending_for_agents
from app.services.company_admissions import fresh_user
from app.services.company_transfer_fence import fence_agents
from app.services.notification_service import send_notification

END_USER_ROLES = frozenset({"member", "agent_admin"})
_QUOTA_EXEMPT_ROLES = frozenset({"platform_admin", "org_admin"})


@dataclass(frozen=True, slots=True)
class OwnershipChange:
    actor: UserRecord
    from_user_id: UUID
    to_user_id: UUID
    agent_ids: tuple[UUID, ...]
    org_admin_only: bool


def _conflict(code: str, message: str, status_code: int = 409) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


async def _party(user_id: UUID, tenant_id: UUID | None) -> UserRecord:
    user = await user_dao.get_with_identity(user_id, fresh=True)
    if user is None or user.tenant_id != tenant_id:
        raise HTTPException(404, {"code": "ownership_user_not_found", "message": "User not found in your company"})
    return user


async def _load_agents(agent_ids: tuple[UUID, ...], tenant_id: UUID | None, from_user_id: UUID) -> list[AgentRecord]:
    by_id = {agent.id: agent for agent in await agent_dao.get_many(list(agent_ids))}
    agents = [by_id[agent_id] for agent_id in agent_ids if agent_id in by_id]
    if len(agents) != len(agent_ids):
        raise HTTPException(404, {"code": "ownership_agent_not_found", "message": "Agent not found"})
    for agent in agents:
        if agent.tenant_id != tenant_id or agent.creator_id != from_user_id:
            raise _conflict("agent_not_owned_by_source", "Agent is not owned by the source user; retry")
        if agent.is_system:
            raise _conflict("system_agent_not_transferable", "System agents cannot be transferred", 403)
    return agents


def _check_parties(change: OwnershipChange, source: UserRecord, target: UserRecord) -> None:
    if change.org_admin_only and source.role not in END_USER_ROLES:
        raise _conflict("ownership_user_role_invalid", "Only members and agent administrators can exchange agents", 422)
    if change.org_admin_only and target.role not in END_USER_ROLES:
        raise _conflict("target_not_eligible", "The new owner must be a member or agent administrator", 422)
    if not target.is_active or target.identity is None or not target.identity.is_active:
        raise _conflict("target_not_eligible", "The new owner must be an active user")


async def _check_quota(target: UserRecord, agents: list[AgentRecord]) -> None:
    if target.role in _QUOTA_EXEMPT_ROLES:
        return
    incoming = sum(1 for agent in agents if not agent.is_expired)
    current = await agent_dao.count_active_for_creator(target.id)
    if current + incoming > target.quota_max_agents:
        raise _conflict(
            "target_quota_exceeded",
            f"Agent limit reached ({current}+{incoming}/{target.quota_max_agents}).",
        )


async def _move_grants(agent: AgentRecord, old_owner: UUID, new_owner: UUID) -> None:
    """Drop the previous owner's personal grants; private/custom agents keep a manage grant for the new owner."""
    new_grant_exists = False
    for grant in await agent_permission_dao.list_for_agent(agent.id):
        if grant.scope_type != "user":
            continue
        if grant.scope_id == old_owner:
            _ = await agent_permission_dao.delete(record_id=grant.id)
        elif grant.scope_id == new_owner:
            new_grant_exists = True
            if grant.access_level != "manage":
                _ = await agent_permission_dao.update(
                    db_obj=grant, obj_in={"access_level": PermissionAccessLevel.MANAGE}
                )
    if not new_grant_exists and (agent.access_mode or "company") in ("private", "custom"):
        _ = await agent_permission_dao.create(
            obj_in={
                "agent_id": agent.id,
                "scope_type": PermissionGrantScope.USER,
                "scope_id": new_owner,
                "access_level": PermissionAccessLevel.MANAGE,
            }
        )


async def apply_ownership_change(change: OwnershipChange) -> OwnershipTransferResponse:
    ids = change.agent_ids
    if len(set(ids)) != len(ids) or change.from_user_id == change.to_user_id:
        raise _conflict("ownership_request_invalid", "Choose different users and unique agents", 422)
    async with connection_ctx() as db:
        actor = await fresh_user(change.actor.id)
        if change.org_admin_only and actor.role != "org_admin":
            raise HTTPException(403, {"code": "ownership_admin_required", "message": "Organization admin required"})
        tenant_id = actor.tenant_id
        _ = await _party(change.from_user_id, tenant_id)
        _ = await _party(change.to_user_id, tenant_id)
        _ = await fence_agents(await _load_agents(ids, tenant_id, change.from_user_id))
        await lock_tenants(db, (tenant_id,))
        for user_id in sorted({actor.id, change.from_user_id, change.to_user_id}):
            await lock_user(db, user_id, tenant_id)
        for agent_id in sorted(ids):
            await lock_agent(db, agent_id, tenant_id)
        source, target = await _party(change.from_user_id, tenant_id), await _party(change.to_user_id, tenant_id)
        _check_parties(change, source, target)
        agents = await _load_agents(ids, tenant_id, source.id)
        await _check_quota(target, agents)
        for agent in agents:
            moved = await agent_dao.update(db_obj=agent, obj_in={"creator_id": target.id})
            await _move_grants(moved, source.id, target.id)
        cancelled = await cancel_pending_for_agents(ids, CancelReason.OWNER_CHANGED)
        await write_admin_audit(
            actor=actor,
            action="agent_ownership.transferred",
            target_type="user",
            target_id=source.id,
            tenant_id=tenant_id,
            details={
                "to_user_id": str(target.id),
                "agent_ids": [str(agent_id) for agent_id in ids],
                "cancelled_deletion_request_ids": [str(request_id) for request_id in cancelled],
            },
        )
        _ = await send_notification(
            target.id,
            notification_kind=NotificationKind.AGENT_OWNERSHIP_TRANSFERRED,
            title=f"{len(ids)} agent(s) transferred to you"[:200],
            body=f"{actor.display_name} made you the owner of {len(ids)} agent(s).",
            link="/app/agents",
        )
        return OwnershipTransferResponse(
            transferred=[str(agent_id) for agent_id in ids],
            cancelled_deletion_request_ids=[str(request_id) for request_id in cancelled],
        )
