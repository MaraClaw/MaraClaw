"""Static channel capability catalog derived from the canonical channel registry."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.domain.channels import ChannelType
from app.core.security import get_current_user
from app.records.user import UserRecord
from app.services.channels.types import CHANNEL_TYPES

router = APIRouter(tags=["channels"])


class ChannelCatalogEntry(BaseModel):
    """Static capabilities of one channel type; never proof that credentials or remote permissions work."""

    channel_type: ChannelType
    display_name: str
    transport: str
    supports_inbound: bool
    supports_proactive: bool


@router.get("/channel-catalog", response_model=list[ChannelCatalogEntry])
async def get_channel_catalog(_current_user: UserRecord = Depends(get_current_user)) -> list[ChannelCatalogEntry]:
    return [
        ChannelCatalogEntry(
            channel_type=kind.channel_type,
            display_name=kind.display_name,
            transport=kind.transport,
            supports_inbound=kind.supports_inbound,
            supports_proactive=kind.supports_proactive,
        )
        for kind in CHANNEL_TYPES.values()
    ]
