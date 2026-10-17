import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import WebSocket
from starlette.datastructures import State
from starlette.types import Message, Scope

from app.api import websocket
from app.services import chat_tickets


async def test_resolve_chat_session_excludes_group_sessions(monkeypatch):
    calls = {}

    async def get_primary_platform(*, agent_id, user_id):
        calls["get_primary"] = {"agent_id": agent_id, "user_id": user_id}
        return

    async def find_web_session(*, agent_id, user_id):
        calls["ensure"] = {"agent_id": agent_id, "user_id": user_id}
        return

    async def create_session(*, obj_in):
        calls["created"] = obj_in
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        websocket.chat_session_dao,
        "get_primary_platform",
        get_primary_platform,
    )
    monkeypatch.setattr(websocket.chat_session_dao, "find_best_web_session", find_web_session)
    monkeypatch.setattr(websocket.chat_session_dao, "create", create_session)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    conv_id = await chat_tickets.resolve_session(chat_tickets.ChatTicketRequest(agent_id=agent_id), user_id)

    assert conv_id is not None
    assert calls["get_primary"]["agent_id"] == agent_id
    assert calls["get_primary"]["user_id"] == user_id
    assert calls["ensure"]["agent_id"] == agent_id
    assert calls["ensure"]["user_id"] == user_id
    assert calls["created"]["source_channel"] == "web"
    assert calls["created"]["is_primary"] is True


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
