from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import google_chat
from app.core.domain.channels import ChannelType
from app.core.json_types import JsonObject
from app.records.channel_config import ChannelConfigRecord
from app.services.channels import google_chat_media, submissions


@pytest.mark.parametrize("display_name", ["", "Unverified sender"])
@pytest.mark.parametrize("attachment_only", [False, True])
async def test_missing_sender_is_acknowledged_without_acceptance(
    monkeypatch: pytest.MonkeyPatch, display_name: str, attachment_only: bool
) -> None:
    # Given
    agent_id = uuid4()
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=agent_id,
        channel_type=ChannelType.GOOGLE_CHAT,
        verification_token="token",
        is_configured=True,
    )
    monkeypatch.setattr(google_chat.channel_config, "get_channel_config", AsyncMock(return_value=config))
    accept_event = AsyncMock()
    accept_media = AsyncMock()
    monkeypatch.setattr(submissions, "accept_channel_event", accept_event)
    monkeypatch.setattr(google_chat_media, "accept_google_chat_media", accept_media)
    body: JsonObject = {
        "type": "MESSAGE",
        "token": "token",
        "message": {
            "name": "spaces/A/messages/B",
            "text": "" if attachment_only else "hello",
            "attachment": [{"name": "spaces/A/attachments/C"}] if attachment_only else [],
            "sender": {"name": None, "email": None, "displayName": display_name, "type": "HUMAN"},
            "space": {"name": "spaces/A", "type": "DM"},
        },
    }
    app = FastAPI()
    app.include_router(google_chat.router, prefix="/api")
    # When
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/channel/google-chat/{agent_id}/webhook", json=body)
    # Then
    assert response.status_code == 200
    assert response.json() == {}
    accept_event.assert_not_awaited()
    accept_media.assert_not_awaited()
