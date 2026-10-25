"""Submit, withdraw, list and read approval-gated agent deletion requests."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.core.acl_locks import lock_tenants
from app.core.notification_kinds import NotificationKind
from app.core.permissions import check_agent_access, is_agent_creator
from app.dao.agent_deletion_request_dao import agent_deletion_request_dao as requests
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.agent_deletion_request import AgentDeletionRequestRecord, CancelReason, DeletionStatus
from app.records.user import UserRecord
from app.schemas.agent_deletion_request import DeletionDetail, DeletionPage, DeletionRequestCreate
from app.services.admin_audit import field_change, write_admin_audit
from app.services.agent_deletion_cursor import DeletionCursor
from app.services.agent_deletion_resolution import lock_request, resolve_request
from app.services.agent_deletion_views import detail, summarize_many
from app.services.company_admissions import fresh_user
from app.services.company_transfer_retention import assert_agent_deletion_allowed
from app.services.notification_service import send_notification


def _forbidden(code: str, message: str) -> HTTPException:
    return HTTPException(403, {"code": code, "message": message})


async def _authorize_submitter(actor: UserRecord, agent_id: UUID) -> AgentRecord:
    if actor.role == "platform_admin":
        raise _forbidden("agent_deletion_request_forbidden", "Platform administrators delete agents directly")
    agent, level = await check_agent_access(actor, agent_id, fresh=True)
    if agent.is_system:
        raise _forbidden("system_agent_not_deletable", "System agents cannot be deleted")
    if agent.tenant_id is None:
        raise _forbidden("agent_deletion_request_forbidden", "Agent has no company")
    if not (is_agent_creator(actor, agent) or (actor.role == "org_admin" and level == "manage")):
        raise _forbidden("agent_deletion_request_forbidden", "Only the owner or a company administrator may request")
    return agent


async def submit(actor: UserRecord, data: DeletionRequestCreate) -> AgentDeletionRequestRecord:
    async with connection_ctx() as db:
        agent = await _authorize_submitter(actor, data.agent_id)
        tenant_id = agent.tenant_id
        if tenant_id is None:
            raise _forbidden("agent_deletion_request_forbidden", "Agent has no company")
        await lock_tenants(db, (tenant_id,))
        await requests.lock_users(db, (actor.id,))
        if not await requests.lock_agent(db, agent.id):
            raise HTTPException(404, "Agent not found")
        requester = await fresh_user(actor.id)
        agent = await _authorize_submitter(requester, data.agent_id)
        await assert_agent_deletion_allowed(db, agent)
        if (pending := await requests.pending_for_agent(agent.id)) is not None:
            raise HTTPException(
                409, {"code": "deletion_request_pending", "message": "Already pending", "request_id": str(pending.id)}
            )
        request = await requests.create(
            obj_in={
                "id": uuid4(),
                "tenant_id": tenant_id,
                "requester_id": requester.id,
                "agent_id": agent.id,
                "agent_name": agent.name,
                "agent_creator_id": agent.creator_id,
                "reason": data.reason,
            }
        )
        for reviewer in await requests.eligible_reviewers(tenant_id):
            if reviewer.id != requester.id:
                _ = await send_notification(
                    reviewer.id,
                    notification_kind=NotificationKind.AGENT_DELETION_REQUESTED,
                    title=f"Agent deletion requested: {agent.name}"[:200],
                    body=f"{requester.display_name} asked to delete {agent.name}. Review the request to respond.",
                    link=f"/agent-deletion-requests/{request.id}",
                    ref_id=request.id,
                )
        await write_admin_audit(
            actor=requester,
            action="agent_deletion.requested",
            target_type="agent_deletion_request",
            target_id=request.id,
            tenant_id=tenant_id,
            details={"agent_id": str(agent.id), "agent_name": agent.name},
        )
        return request


async def withdraw(request_id: UUID, actor: UserRecord) -> AgentDeletionRequestRecord:
    async with connection_ctx() as db:
        locked = await lock_request(db, request_id, (actor.id,))
        if locked is None or locked[0].requester_id != actor.id or locked[0].tenant_id != actor.tenant_id:
            raise HTTPException(404, "Deletion request not found")
        request = locked[0]
        if request.status == DeletionStatus.CANCELLED and request.cancel_reason == CancelReason.WITHDRAWN:
            return request
        if request.status != DeletionStatus.PENDING:
            raise HTTPException(409, {"code": "deletion_request_stale", "message": "Request is resolved"})
        resolved = await resolve_request(request, DeletionStatus.CANCELLED, CancelReason.WITHDRAWN)
        await write_admin_audit(
            actor=actor,
            action="agent_deletion.withdrawn",
            target_type="agent_deletion_request",
            target_id=request.id,
            tenant_id=request.tenant_id,
            changes={"status": field_change(request.status.value, resolved.status.value)},
        )
        return resolved


async def get_detail(request_id: UUID, actor: UserRecord) -> DeletionDetail:
    request = await requests.get(request_id)
    if request is None or request.tenant_id != actor.tenant_id:
        raise HTTPException(404, "Deletion request not found")
    reviewer = await requests.eligible_reviewer(request.tenant_id, actor.id)
    if reviewer is None and request.requester_id != actor.id:
        raise HTTPException(404, "Deletion request not found")
    if reviewer is not None and request.status == DeletionStatus.PENDING:
        await requests.record_view(request.id, actor.id)
    return await detail(request, actor.id)


@dataclass(frozen=True, slots=True)
class DeletionQuery:
    status: DeletionStatus | None
    agent_id: UUID | None
    limit: int
    cursor: str | None


async def list_page(actor: UserRecord, query: DeletionQuery) -> DeletionPage:
    status, agent_id, limit, cursor = query.status, query.agent_id, query.limit, query.cursor
    if actor.tenant_id is None:
        raise HTTPException(403, "Company membership required")
    reviewer = await requests.eligible_reviewer(actor.tenant_id, actor.id)
    requester_id = None if reviewer is not None else actor.id
    position = DeletionCursor.decode(cursor) if cursor is not None else None
    if position is not None and (
        (position.tenant_id, position.requester_id, position.status, position.agent_id)
        != (actor.tenant_id, requester_id, status, agent_id)
    ):
        raise HTTPException(400, "Invalid cursor")
    rows = await requests.list_page(
        tenant_id=actor.tenant_id,
        requester_id=requester_id,
        status=status,
        agent_id=agent_id,
        before=(position.created_at, position.id) if position is not None else None,
        limit=limit,
    )
    items = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = DeletionCursor(
            version=1,
            tenant_id=actor.tenant_id,
            requester_id=requester_id,
            status=status,
            agent_id=agent_id,
            created_at=last.created_at,
            id=last.id,
        ).encode()
    return DeletionPage(items=await summarize_many(items), next_cursor=next_cursor)
