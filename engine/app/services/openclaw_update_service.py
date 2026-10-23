"""OpenClaw upgrade notices, per-agent update state and upgrade requests."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status

from app.core.permissions import compute_agent_access_level, list_visible_agents
from app.dao.openclaw_update_dao import openclaw_update_notice_dao, openclaw_update_run_dao
from app.records.agent import AgentRecord
from app.records.openclaw_update import OpenClawUpdateNoticeRecord, OpenClawUpdateRunRecord
from app.records.user import UserRecord
from app.schemas.openclaw_updates import (
    AgentUpdateOut,
    NoticeAgentOut,
    NoticeDetailOut,
    OpenClawNoticeOut,
    OpenClawRunOut,
    PublishNoticeIn,
)

NOT_RUNNING_CODE = "openclaw_update_not_running"
_SENDER_LIMIT = 100


def notice_out(notice: OpenClawUpdateNoticeRecord) -> OpenClawNoticeOut:
    return OpenClawNoticeOut(
        id=notice.id, title=notice.title, body=notice.body, mandatory=notice.mandatory, created_at=notice.created_at
    )


def run_out(run: OpenClawUpdateRunRecord) -> OpenClawRunOut:
    return OpenClawRunOut(id=run.id, state=run.state, failure_code=run.failure_code)


async def publish_notice(actor: UserRecord, request: PublishNoticeIn) -> tuple[OpenClawUpdateNoticeRecord, bool]:
    sender = (actor.display_name or actor.email or "Platform admin")[:_SENDER_LIMIT]
    return await openclaw_update_notice_dao.publish(
        request_id=request.request_id,
        title=request.title,
        body=request.body,
        mandatory=request.mandatory,
        actor_id=actor.id,
        sender_name=sender,
    )


async def agent_update(agent: AgentRecord, access_level: str | None) -> AgentUpdateOut:
    """Default gate view: newest outstanding mandatory notice, else the latest optional one."""
    notice = await openclaw_update_notice_dao.selected_for_agent(agent.id)
    run = await openclaw_update_run_dao.active_for_agent(agent.id)
    if run is None and notice is not None:
        run = await openclaw_update_run_dao.latest_for_notice(agent.id, notice.id)
    return AgentUpdateOut(
        notice=notice_out(notice) if notice is not None else None,
        run=run_out(run) if run is not None else None,
        required=notice is not None and notice.mandatory,
        can_upgrade=access_level == "manage",
    )


async def notice_detail(user: UserRecord, notice_id: UUID) -> NoticeDetailOut:
    notice = await openclaw_update_notice_dao.get(notice_id)
    if notice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Update notice not found")
    agents: list[NoticeAgentOut] = []
    for agent in await list_visible_agents(user):
        level = await compute_agent_access_level(user, agent)
        if level in ("manage", "use"):
            agents.append(
                NoticeAgentOut(
                    agent_id=agent.id, name=agent.name, status=agent.status, update=await agent_update(agent, level)
                )
            )
    return NoticeDetailOut(notice=notice_out(notice), agents=agents)


async def request_upgrade(
    agent: AgentRecord, notice_id: UUID, actor: UserRecord
) -> tuple[OpenClawUpdateRunRecord, bool]:
    """Return ``(run, created)``; an existing active or succeeded run replays."""
    if await openclaw_update_notice_dao.get(notice_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Update notice not found")
    for existing in (
        await openclaw_update_run_dao.succeeded_for(agent.id, notice_id),
        await openclaw_update_run_dao.active_for_agent(agent.id),
    ):
        if existing is not None:
            return existing, False
    if agent.status not in ("running", "idle") or agent.container_id in (None, "") or agent.setup_pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": NOT_RUNNING_CODE})
    created = await openclaw_update_run_dao.create_pending(
        notice_id=notice_id, agent_id=agent.id, requested_by=actor.id
    )
    if created is not None:
        return created, True
    raced = await openclaw_update_run_dao.active_for_agent(agent.id)
    if raced is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upgrade state changed; retry")
    return raced, False
