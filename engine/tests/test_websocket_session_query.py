import uuid
from types import SimpleNamespace

from fastapi import WebSocket
from starlette.datastructures import State
from starlette.types import Message, Scope

from app.api import websocket


async def test_resolve_chat_session_excludes_group_sessions(monkeypatch):
    calls = {}

    async def get_primary_platform(*, agent_id, user_id):
        calls["get_primary"] = {"agent_id": agent_id, "user_id": user_id}
        return

    async def ensure_primary_session(db, agent_id, user_id):
        calls["ensure"] = {"agent_id": agent_id, "user_id": user_id}
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        websocket.chat_session_dao,
        "get_primary_platform",
        get_primary_platform,
    )
    monkeypatch.setattr(websocket, "ensure_primary_platform_session", ensure_primary_session)

    async def receive() -> Message:
        return {"type": "websocket.disconnect"}

    async def send(_message: Message) -> None:
        return None

    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "scheme": "ws",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("test", 80),
        "server": ("test", 80),
        "subprotocols": [],
    }
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    handler = websocket.WebSocketChatHandler(WebSocket[State](scope, receive, send), agent_id, "token")

    conv_id = await handler._resolve_chat_session(None, user_id)

    assert conv_id is not None
    assert calls["get_primary"]["agent_id"] == agent_id
    assert calls["get_primary"]["user_id"] == user_id
    assert calls["ensure"]["agent_id"] == agent_id
    assert calls["ensure"]["user_id"] == user_id
