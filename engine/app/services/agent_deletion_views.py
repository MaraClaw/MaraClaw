"""Response assembly for agent deletion requests (reviewer metadata only, never chats/files/secrets)."""

from collections.abc import Sequence
from uuid import UUID

from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.dao.agent_deletion_request_dao import agent_deletion_request_dao as requests
from app.dao.user_dao import user_dao
from app.records.agent_deletion_request import AgentDeletionRequestRecord, DeletionStatus
from app.schemas.agent_deletion_request import (
    DeletionAgent,
    DeletionApproval,
    DeletionDetail,
    DeletionProgress,
    DeletionSummary,
    DeletionViewer,
    PersonRef,
)


async def _names(user_ids: Sequence[UUID]) -> dict[UUID, str]:
    return {user.id: user.display_name for user in await user_dao.get_many(list(set(user_ids)))}


async def summarize_many(items: Sequence[AgentDeletionRequestRecord]) -> list[DeletionSummary]:
    names = await _names([item.requester_id for item in items])
    progress = await requests.progress([item.id for item in items])
    summaries: list[DeletionSummary] = []
    for item in items:
        counted, genesis = progress.get(item.id, (0, False))
        summaries.append(
            DeletionSummary(
                id=item.id,
                agent_id=item.agent_id,
                agent_name=item.agent_name,
                status=item.status.value,
                cancel_reason=item.cancel_reason.value if item.cancel_reason is not None else None,
                requester=PersonRef(id=item.requester_id, display_name=names.get(item.requester_id, "")),
                created_at=item.created_at,
                resolved_at=item.resolved_at,
                progress=DeletionProgress(counted_approvals=counted, genesis_approved=genesis),
            )
        )
    return summaries


async def _live_agent(item: AgentDeletionRequestRecord) -> DeletionAgent | None:
    memo_drop("agent", item.agent_id)
    agent = await agent_dao.get(item.agent_id)
    if agent is None or agent.tenant_id != item.tenant_id:
        return None
    creator = await user_dao.get(agent.creator_id)
    return DeletionAgent(
        id=agent.id,
        name=agent.name,
        role_description=agent.role_description,
        access_mode=agent.access_mode or "company",
        status=agent.status,
        last_active_at=agent.last_active_at,
        creator=PersonRef(id=agent.creator_id, display_name=creator.display_name if creator else ""),
        is_private=(agent.access_mode or "company") == "private",
    )


async def detail(item: AgentDeletionRequestRecord, viewer_id: UUID) -> DeletionDetail:
    [summary] = await summarize_many([item])
    approvals = await requests.approvals(item)
    eligible = await requests.eligible_reviewer(item.tenant_id, viewer_id) is not None
    has_approved = any(approval.reviewer_id == viewer_id for approval in approvals)
    return DeletionDetail(
        id=summary.id,
        agent_id=summary.agent_id,
        agent_name=summary.agent_name,
        status=summary.status,
        cancel_reason=summary.cancel_reason,
        requester=summary.requester,
        created_at=summary.created_at,
        resolved_at=summary.resolved_at,
        progress=summary.progress,
        reason=item.reason,
        agent=await _live_agent(item),
        viewer=DeletionViewer(
            can_approve=eligible and item.status == DeletionStatus.PENDING and not has_approved,
            has_viewed=await requests.has_viewed(item.id, viewer_id),
            has_approved=has_approved,
        ),
        approvals=[
            DeletionApproval(
                reviewer_id=approval.reviewer_id,
                display_name=approval.display_name,
                is_genesis=approval.is_genesis,
                approved_at=approval.approved_at,
                counted=approval.counted,
            )
            for approval in approvals
        ],
    )
