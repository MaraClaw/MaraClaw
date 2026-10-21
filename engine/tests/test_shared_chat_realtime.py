import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, WebSocket
from starlette.types import Message

from app.api.websocket import ConnectionManager, WebSocketConnection
from app.services import chat_tickets
from app.services.realtime_runtime.router import RealtimeRouter
from app.services.shared_chat import shared_session_id


def socket_with_binding(agent_id, user_id, frames):
    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        frames.append(message)

    socket = WebSocket({"type": "websocket", "headers": []}, receive, send)
    socket.state.chat_binding = chat_tickets.ChatTicketBinding(
        user_id=user_id,
        tenant_id=uuid4(),
        agent_id=agent_id,
        session_id=shared_session_id(agent_id),
        auth_deadline=int(time.time()) + 3600,
    )
    return socket


@pytest.mark.parametrize("remote", [False, True])
async def test_passive_shared_delivery_reauthorizes_each_recipient(monkeypatch, remote):
    agent_id, user_id = uuid4(), uuid4()
    frames: list[Message] = []
    socket = socket_with_binding(agent_id, user_id, frames)
    await socket.accept()
    frames.clear()
    authorize = AsyncMock(side_effect=HTTPException(403, "Permission revoked"))
    monkeypatch.setattr(chat_tickets, "authorize", authorize)
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    connections: list[WebSocketConnection] = [(socket, str(shared_session_id(agent_id)), str(user_id))]

    if remote:
        manager = ConnectionManager()
        manager.active_connections[str(agent_id)] = connections
        await manager.deliver_pubsub_message(
            agent_id=str(agent_id), session_id=str(shared_session_id(agent_id)), payload={"type": "done"}
        )
    else:
        router = RealtimeRouter()
        monkeypatch.setattr(router, "_list_presence", AsyncMock(return_value=[]))
        await router.route_message(
            agent_id=str(agent_id),
            session_id=str(shared_session_id(agent_id)),
            message={"type": "done"},
            local_connections=connections,
        )

    assert not frames
    authorize.assert_awaited_once_with(socket.state.chat_binding)


async def test_shared_route_keeps_session_scope_with_remote_only_viewer(monkeypatch):
    agent_id = uuid4()
    route = AsyncMock()
    monkeypatch.setattr("app.api.websocket.realtime_router.route_message", route)

    await ConnectionManager().send_to_session(
        str(agent_id), str(shared_session_id(agent_id)), {"type": "done"}, user_id=str(uuid4())
    )

    assert route.await_args is not None
    assert route.await_args.kwargs["session_id"] == str(shared_session_id(agent_id))
    assert route.await_args.kwargs.get("user_id") is None


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("binding_state", ["current", "missing", "wrong_session"])
async def test_shared_delivery_requires_current_matching_binding(monkeypatch, remote, binding_state):
    agent_id, user_id = uuid4(), uuid4()
    frames: list[Message] = []
    socket = socket_with_binding(agent_id, user_id, frames)
    await socket.accept()
    frames.clear()
    if binding_state == "missing":
        del socket.state.chat_binding
    elif binding_state == "wrong_session":
        socket.state.chat_binding.session_id = uuid4()
    authorize = AsyncMock()
    session = AsyncMock()
    monkeypatch.setattr(chat_tickets, "authorize", authorize)
    monkeypatch.setattr(chat_tickets, "authorized_session", session)
    connections: list[WebSocketConnection] = [(socket, str(shared_session_id(agent_id)), str(user_id))]

    if remote:
        manager = ConnectionManager()
        manager.active_connections[str(agent_id)] = connections
        await manager.deliver_pubsub_message(
            agent_id=str(agent_id), session_id=str(shared_session_id(agent_id)), payload={"type": "done"}
        )
    else:
        router = RealtimeRouter()
        monkeypatch.setattr(router, "_list_presence", AsyncMock(return_value=[]))
        await router.route_message(
            agent_id=str(agent_id),
            session_id=str(shared_session_id(agent_id)),
            message={"type": "done"},
            local_connections=connections,
        )

    assert len(frames) == (1 if binding_state == "current" else 0)
    if binding_state == "current":
        authorize.assert_awaited_once_with(socket.state.chat_binding)
        session.assert_awaited_once_with(socket.state.chat_binding)
    else:
        authorize.assert_not_awaited()


async def test_shared_redis_envelope_excludes_archive_presence_and_user_fallback(monkeypatch):
    agent = uuid4()
    agent_id = str(agent)
    session_id = str(shared_session_id(agent))
    router = RealtimeRouter()
    publish = AsyncMock()
    monkeypatch.setattr(
        "app.services.realtime_runtime.router.get_redis", AsyncMock(return_value=SimpleNamespace(publish=publish))
    )
    monkeypatch.setattr(
        router,
        "_list_presence",
        AsyncMock(
            return_value=[
                {"instance_id": "shared-remote", "session_id": session_id},
                {"instance_id": "private-remote", "session_id": str(uuid4())},
            ]
        ),
    )

    await router.route_message(agent_id=agent_id, session_id=session_id, message={"type": "done"}, local_connections=[])

    publish.assert_awaited_once()
    assert publish.await_args is not None
    channel, envelope = publish.await_args.args
    assert channel == "realtime:ws:instance:shared-remote"
    assert json.loads(envelope)["session_id"] == session_id
    assert json.loads(envelope)["user_id"] is None
