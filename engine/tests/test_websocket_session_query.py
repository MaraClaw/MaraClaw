import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket
from starlette.datastructures import State
from starlette.types import Message, Scope

from app.api import websocket
from app.records.chat import ChatSessionRecord
from app.services import chat_session_service, chat_tickets
from app.services.shared_chat import shared_session_id


async def test_resolve_chat_session_excludes_group_sessions(monkeypatch):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    shared = ChatSessionRecord(
        id=shared_session_id(agent_id), agent_id=agent_id, user_id=user_id, source_channel="shared", is_primary=True
    )
    ensure = AsyncMock(return_value=shared)
    monkeypatch.setattr(chat_session_service, "ensure_shared_session", ensure)

    conv_id = await chat_tickets.resolve_session(chat_tickets.ChatTicketRequest(agent_id=agent_id), user_id)

    assert conv_id == shared.id
    assert not shared.is_group
    ensure.assert_awaited_once_with(agent_id)


@pytest.mark.asyncio
async def test_send_to_session_falls_back_to_user(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    async def route_message(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(websocket.realtime_router, "route_message", route_message)
    manager = websocket.ConnectionManager()
    scope: Scope = {"type": "websocket"}

    async def receive() -> Message:
        return {"type": "websocket.disconnect"}

    async def send(_message: Message) -> None:
        return None

    ws = WebSocket[State](scope, receive, send)
    user_id = str(uuid.uuid4())
    manager.active_connections["agent-1"] = [(ws, "other-session", user_id)]

    await manager.send_to_session(
        "agent-1",
        "wanted-session",
        {"type": "done", "content": "hi"},
        user_id=user_id,
    )

    assert sent[0]["user_id"] == user_id
    assert sent[0]["local_connections"] == [(ws, "other-session", user_id)]
