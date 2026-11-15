from uuid import UUID

from fastapi import APIRouter, Depends, Response

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.channel_links import ChannelLinkCodeResponse, ChannelLinkStatus, LinkChannel
from app.services.channels import sender_links

router = APIRouter(tags=["channel-links"])


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


@router.get("/agents/{agent_id}/channel-links/{channel}", response_model=ChannelLinkStatus)
async def get_channel_link(
    agent_id: UUID, channel: LinkChannel, response: Response, current_user: UserRecord = Depends(get_current_user)
) -> ChannelLinkStatus:
    _private(response)
    return await sender_links.link_status(agent_id, channel, current_user)


@router.post("/agents/{agent_id}/channel-links/{channel}/code", response_model=ChannelLinkCodeResponse)
async def create_channel_link_code(
    agent_id: UUID, channel: LinkChannel, response: Response, current_user: UserRecord = Depends(get_current_user)
) -> ChannelLinkCodeResponse:
    _private(response)
    return await sender_links.issue_link_code(agent_id, channel, current_user)


@router.delete("/agents/{agent_id}/channel-links/{channel}", status_code=204)
async def delete_channel_link(
    agent_id: UUID, channel: LinkChannel, response: Response, current_user: UserRecord = Depends(get_current_user)
) -> None:
    _private(response)
    await sender_links.unlink(agent_id, channel, current_user)
