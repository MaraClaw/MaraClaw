from collections.abc import Coroutine
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import google_chat
from app.core.domain.channels import ChannelType
from app.core.json_types import JsonObject
from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("display_name", ["", "Unverified sender"])
@pytest.mark.parametrize("attachment_only", [False, True])
async def test_missing_sender_is_acknowledged_without_transcript_access(
    monkeypatch: pytest.MonkeyPatch, use_async: bool, display_name: str, attachment_only: bool
) -> None:
    # Given
    agent = AgentRecord(id=uuid4(), name="Assistant", creator_id=uuid4())
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=agent.id,
        channel_type=ChannelType.GOOGLE_CHAT,
        verification_token="token",
        is_configured=True,
        extra_config={"service_account_json": {"client_email": "bot@example.com"}} if use_async else {},
    )
    monkeypatch.setattr(google_chat.channel_config, "get_channel_config", AsyncMock(return_value=config))
    monkeypatch.setattr(google_chat.channel_inbound, "load_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(google_chat.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(google_chat.channel_dedup, "mark_processed_shared", AsyncMock())
    blocked = {
        name: AsyncMock(side_effect=AssertionError(f"Missing sender reached {name}"))
        for name in (
            "resolve_sender_user",
            "open_channel_session",
            "bind_channel_conversation",
            "load_history_for_session",
            "persist_user_message",
            "persist_assistant_message",
            "generate_channel_reply",
        )
    }
    for name, operation in blocked.items():
        monkeypatch.setattr(google_chat.channel_inbound, name, operation)
    pending: list[Coroutine[None, None, None]] = []

    def schedule(coroutine: Coroutine[None, None, None], description: str) -> None:
        pending.append(coroutine)

    monkeypatch.setattr(google_chat, "schedule_background_task", schedule)
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
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/channel/google-chat/{agent.id}/webhook", json=body)
        for coroutine in pending:
            await coroutine
    finally:
        for coroutine in pending:
            coroutine.close()
    # Then
    assert response.status_code == 200
    assert response.json() == {}
    for operation in blocked.values():
        operation.assert_not_awaited()
