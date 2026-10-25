import uuid

import httpx
from fastapi import APIRouter, FastAPI

from app.core.domain.channels import ChannelType
from app.records.channel_config import ChannelConfigRecord

AGENT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def channel_config(channel_type: str) -> ChannelConfigRecord:
    return ChannelConfigRecord(
        id=uuid.uuid4(),
        agent_id=AGENT_ID,
        channel_type=ChannelType(channel_type),
        app_id="bot-app",
        app_secret="access-token",
        verification_token="verification-token",
        encrypt_key="app-secret",
        is_configured=True,
    )


def webhook_client(router: APIRouter) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
