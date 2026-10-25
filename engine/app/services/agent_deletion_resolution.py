"""Shared locking, resolution and notification steps for agent deletion requests."""

from collections.abc import Sequence
from uuid import UUID

from app.core.acl_locks import lock_tenants
from app.core.notification_kinds import NotificationKind
from app.dao.agent_deletion_request_dao import agent_deletion_request_dao as requests
from app.db.connection import DbConnection
from app.records.agent_deletion_request import AgentDeletionRequestRecord, CancelReason, DeletionStatus
from app.services.notification_service import send_notification

_TITLES = {
    DeletionStatus.EXECUTED: "Agent deleted",
    DeletionStatus.CANCELLED: "Agent deletion request cancelled",
}


async def lock_request(
    db: DbConnection, request_id: UUID, extra_users: Sequence[UUID] = ()
) -> tuple[AgentDeletionRequestRecord, bool] | None:
    """Lock tenant, sorted users, the agent, then the request, in the platform ACL order.

    Returns the locked request and whether the agent row still exists.
    """
    snapshot = await requests.get(request_id)
    if snapshot is None:
        return None
    await lock_tenants(db, (snapshot.tenant_id,))
    # Approvals are only written under the tenant mutex, so this set is stable from here on.
    approvers = [approval.reviewer_id for approval in await requests.approvals(snapshot)]
    await requests.lock_users(db, (snapshot.requester_id, *extra_users, *approvers))
    agent_exists = await requests.lock_agent(db, snapshot.agent_id)
    locked = await requests.get_locked(request_id)
    return (locked, agent_exists) if locked is not None else None


async def resolve_request(
    request: AgentDeletionRequestRecord, status: DeletionStatus, cancel_reason: CancelReason | None = None
) -> AgentDeletionRequestRecord:
    resolved = await requests.resolve(request.id, status, cancel_reason)
    outcome = cancel_reason.value.replace("_", " ") if cancel_reason is not None else "approved"
    await send_notification(
        request.requester_id,
        notification_kind=NotificationKind.AGENT_DELETION_RESOLVED,
        title=f"{_TITLES[status]}: {request.agent_name}"[:200],
        body=f"Deletion of {request.agent_name} is {status.value} ({outcome}).",
        link="/app/agents",
        ref_id=request.id,
    )
    return resolved


async def cancel_pending_for_agents(agent_ids: Sequence[UUID], reason: CancelReason) -> list[UUID]:
    """Cancel pending requests for agents the caller has already locked."""
    cancelled = await requests.pending_for_agents_locked(agent_ids)
    for request in cancelled:
        _ = await resolve_request(request, DeletionStatus.CANCELLED, reason)
    return [request.id for request in cancelled]
