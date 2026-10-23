"""OpenClaw upgrade endpoints: platform-admin publication, member notice page, per-agent upgrade."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.permissions import check_agent_access
from app.core.security import get_current_user, require_role
from app.records.user import UserRecord
from app.schemas.openclaw_updates import (
    AgentUpdateOut,
    NoticeDetailOut,
    OpenClawRunOut,
    PublishNoticeIn,
    PublishNoticeOut,
    RequestUpgradeIn,
)
from app.services import openclaw_update_service as updates

router = APIRouter(tags=["openclaw-updates"])


@router.post("/admin/openclaw-updates", status_code=status.HTTP_201_CREATED)
async def publish_openclaw_update(
    body: PublishNoticeIn,
    response: Response,
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> PublishNoticeOut:
    """Publish one global notice and notify every eligible member; the same ``request_id`` replays (200)."""
    notice, created = await updates.publish_notice(current_user, body)
    if not created:
        response.status_code = status.HTTP_200_OK
    return PublishNoticeOut(notice=updates.notice_out(notice), recipients=notice.recipients)


@router.get("/openclaw-updates/{notice_id}")
async def get_openclaw_update(
    notice_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> NoticeDetailOut:
    """The notice plus every agent the caller can access, with its own update state."""
    return await updates.notice_detail(current_user, notice_id)


@router.get("/agents/{agent_id}/openclaw-update")
async def get_agent_openclaw_update(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> AgentUpdateOut:
    agent, level = await check_agent_access(current_user, agent_id)
    return await updates.agent_update(agent, level)


@router.post("/agents/{agent_id}/openclaw-update", status_code=status.HTTP_202_ACCEPTED)
async def request_agent_openclaw_update(
    agent_id: uuid.UUID,
    body: RequestUpgradeIn,
    response: Response,
    current_user: UserRecord = Depends(get_current_user),
) -> OpenClawRunOut:
    """Queue the worker-run upgrade (202); an existing active or succeeded run replays (200)."""
    agent, level = await check_agent_access(current_user, agent_id)
    if level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can upgrade agent")
    run, created = await updates.request_upgrade(agent, body.notice_id, current_user)
    if not created:
        response.status_code = status.HTTP_200_OK
    return updates.run_out(run)
