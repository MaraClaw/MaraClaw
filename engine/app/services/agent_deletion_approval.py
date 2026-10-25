"""Approve agent deletion requests and execute them once the threshold is met."""

from uuid import UUID

from fastapi import HTTPException

from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.dao.agent_deletion_request_dao import agent_deletion_request_dao as requests
from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.agent_deletion_request import AgentDeletionRequestRecord, CancelReason, DeletionStatus
from app.records.user import UserRecord
from app.schemas.agent_deletion_request import DeletionDetail
from app.services.admin_audit import field_change, write_admin_audit
from app.services.agent_deletion_resolution import lock_request, resolve_request
from app.services.agent_deletion_views import detail
from app.services.agent_stops import schedule_agent_teardown, snapshot_agent_teardown
from app.services.company_transfer_retention import assert_agent_deletion_allowed

_REQUIRED_APPROVALS = 2


def _stale_reason(request: AgentDeletionRequestRecord, agent: AgentRecord | None) -> CancelReason | None:
    if agent is None:
        return CancelReason.AGENT_GONE
    if agent.tenant_id != request.tenant_id:
        return CancelReason.TENANT_CHANGED
    if agent.creator_id != request.agent_creator_id:
        return CancelReason.OWNER_CHANGED
    return None


async def _audit(actor: UserRecord, action: str, request: AgentDeletionRequestRecord, status: str) -> None:
    await write_admin_audit(
        actor=actor,
        action=action,
        target_type="agent_deletion_request",
        target_id=request.id,
        tenant_id=request.tenant_id,
        changes={"status": field_change(request.status.value, status)},
        details={"agent_id": str(request.agent_id), "agent_name": request.agent_name},
    )


async def _execute(
    db: DbConnection, actor: UserRecord, request: AgentDeletionRequestRecord, agent: AgentRecord
) -> AgentDeletionRequestRecord:
    if agent.is_system:
        raise HTTPException(403, {"code": "system_agent_not_deletable", "message": "System agents cannot be deleted"})
    await assert_agent_deletion_allowed(db, agent)
    resolved = await resolve_request(request, DeletionStatus.EXECUTED)
    teardown = await snapshot_agent_teardown([agent])
    await agent_dao.delete_with_related(agent.id)
    schedule_agent_teardown(teardown)
    await _audit(actor, "agent_deletion.executed", request, resolved.status.value)
    return resolved


async def approve(request_id: UUID, actor: UserRecord) -> DeletionDetail:
    async with connection_ctx() as db:
        locked = await lock_request(db, request_id, (actor.id,))
        if locked is None or locked[0].tenant_id != actor.tenant_id:
            raise HTTPException(404, "Deletion request not found")
        request, _agent_exists = locked
        reviewer = await requests.eligible_reviewer(request.tenant_id, actor.id)
        if reviewer is None:
            raise HTTPException(
                403, {"code": "reviewer_not_eligible", "message": "Active company administrator required"}
            )
        if request.status == DeletionStatus.EXECUTED:
            return await detail(request, actor.id)
        if request.status == DeletionStatus.CANCELLED:
            raise HTTPException(409, {"code": "deletion_request_stale", "message": "Request is cancelled"})
        memo_drop("agent", request.agent_id)
        agent = await agent_dao.get(request.agent_id)
        if (stale := _stale_reason(request, agent)) is not None:
            resolved = await resolve_request(request, DeletionStatus.CANCELLED, stale)
            await _audit(actor, "agent_deletion.cancelled", request, resolved.status.value)
            return await detail(resolved, actor.id)
        if agent is None:
            raise HTTPException(404, "Agent not found")
        if not await requests.has_viewed(request.id, actor.id):
            raise HTTPException(
                409, {"code": "detail_view_required", "message": "Open the request details before approving"}
            )
        if await requests.record_approval(request.id, actor.id):
            await _audit(actor, "agent_deletion.approved", request, request.status.value)
        counted = [approval for approval in await requests.approvals(request) if approval.counted]
        if any(approval.is_genesis for approval in counted) or len(counted) >= _REQUIRED_APPROVALS:
            request = await _execute(db, actor, request, agent)
        return await detail(request, actor.id)
