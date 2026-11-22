import json
import time
import uuid
from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket
from starlette.types import Message
from tests.realtime_pipeline_fixtures import HashPipeline

from app.api import websocket
from app.core import access_cache
from app.records.chat import ChatSessionRecord
from app.services import chat_tickets
from app.services.realtime_runtime import chat_authorization, router as realtime


async def test_resolve_chat_session_excludes_group_sessions(monkeypatch):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    canonical = ChatSessionRecord(
        id=uuid.uuid4(), agent_id=agent_id, user_id=user_id, source_channel="web", is_primary=True
    )
    ensure = AsyncMock(return_value=canonical)
    monkeypatch.setattr(chat_tickets, "ensure_primary_platform_session", ensure)

    conv_id = await chat_tickets.resolve_session(chat_tickets.ChatTicketRequest(agent_id=agent_id), user_id)

    assert conv_id == canonical.id
    assert not canonical.is_group
    ensure.assert_awaited_once_with(agent_id, user_id)


class MemoryPresence:
    def __init__(self, agent_id: str, records: dict[str, dict[str, str]]) -> None:
        self.members: dict[str, set[str]] = {f"realtime:ws:agent:{agent_id}": set(records)}
        self.hashes: dict[str, dict[str, str]] = {
            f"realtime:ws:conn:{connection_id}": record for connection_id, record in records.items()
        }
        self.published: list[tuple[str, str]] = []

    async def smembers(self, key: str) -> set[str]:
        return self.members.get(key, set())

    async def hgetall(self, key: str) -> dict[str, str]:
        return self.hashes.get(key, {})

    def pipeline(self, transaction: bool = True) -> HashPipeline:
        _ = transaction
        return HashPipeline(self.hgetall)

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


async def connected_socket() -> tuple[WebSocket, list[Message]]:
    frames: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        frames.append(message)

    socket = WebSocket({"type": "websocket", "headers": []}, receive, send)
    await socket.accept()
    frames.clear()
    return socket, frames


def bind(socket: WebSocket, agent_id: str, session_id: str, user_id: str, *, canonical: bool) -> None:
    """Give the socket the binding its ticket carries; a canonical session's ticket authorizes each delivery."""
    socket.state.chat_binding = chat_tickets.ChatTicketBinding(
        user_id=uuid.UUID(user_id),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.UUID(agent_id),
        session_id=uuid.UUID(session_id),
        auth_deadline=int(time.time()) + 3600,
        auth_generation=0,
        authorize_deliveries=canonical,
    )


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("local_target", [False, True])
@pytest.mark.parametrize("restrict_user", [False, True])
async def test_send_to_session_isolates_local_and_remote_recipients(
    monkeypatch: pytest.MonkeyPatch, local_target: bool, restrict_user: bool, canonical: bool
) -> None:
    agent_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    other_session_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    other_user_id = str(uuid.uuid4())
    manager = websocket.ConnectionManager()
    remote_manager = websocket.ConnectionManager()
    router = realtime.RealtimeRouter()
    router.instance_id = "local"
    monkeypatch.setattr(websocket, "realtime_router", router)

    local_socket, local_frames = await connected_socket()
    local_other_session, local_other_session_frames = await connected_socket()
    local_other_user, local_other_user_frames = await connected_socket()
    manager.active_connections[agent_id] = [(local_other_session, other_session_id, user_id)]
    if local_target:
        manager.active_connections[agent_id].extend(
            [(local_socket, session_id, user_id), (local_other_user, session_id, other_user_id)]
        )
    remote_socket, remote_frames = await connected_socket()
    remote_other_session, remote_other_session_frames = await connected_socket()
    remote_other_user, remote_other_user_frames = await connected_socket()
    remote_manager.active_connections[agent_id] = [
        (remote_socket, session_id, user_id),
        (remote_other_session, other_session_id, user_id),
        (remote_other_user, session_id, other_user_id),
    ]
    for connections in (manager.active_connections[agent_id], remote_manager.active_connections[agent_id]):
        for socket, socket_session, socket_user in connections:
            assert socket_session is not None
            assert socket_user is not None
            bind(socket, agent_id, socket_session, socket_user, canonical=canonical)
    authorize = AsyncMock(return_value=(object(), object(), "use"))
    read_version = AsyncMock(return_value="1")
    monkeypatch.setattr(chat_tickets, "authorize_identity", authorize)
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    monkeypatch.setattr(access_cache, "read_acl_version", read_version)
    monkeypatch.setattr(chat_authorization, "connection_ctx", nullcontext)
    store = MemoryPresence(
        agent_id,
        {
            "target": {"instance_id": "remote", "session_id": session_id, "user_id": user_id},
            "other-session": {
                "instance_id": "other-session-only",
                "session_id": other_session_id,
                "user_id": user_id,
            },
            "other-user": {"instance_id": "remote", "session_id": session_id, "user_id": other_user_id},
        },
    )

    async def get_redis() -> MemoryPresence:
        return store

    monkeypatch.setattr(realtime, "get_redis", get_redis)
    message: websocket.RealtimeMessage = {"type": "done", "content": "private reply"}
    recipient_user_id = user_id if restrict_user else None

    await manager.send_to_session(agent_id, f" {session_id.upper()} ", message, user_id=recipient_user_id)
    for _channel, envelope in store.published:
        await realtime.RealtimeRouter._deliver_message(envelope, remote_manager.deliver_pubsub_message)

    expected_frame = {"type": "websocket.send", "text": json.dumps(message, separators=(",", ":"), ensure_ascii=False)}
    assert local_frames == ([expected_frame] if local_target else [])
    assert local_other_session_frames == []
    assert local_other_user_frames == ([expected_frame] if local_target and not restrict_user else [])
    assert remote_frames == [expected_frame]
    assert remote_other_session_frames == []
    assert remote_other_user_frames == ([] if restrict_user else [expected_frame])
    # Only sockets bound to a canonical session authorize, and only the ones the filters let through.
    decided = {call.args[0].user_id for call in authorize.await_args_list}
    if not canonical:
        assert decided == set()
    elif restrict_user:
        assert decided == {uuid.UUID(user_id)}
    else:
        assert decided == {uuid.UUID(user_id), uuid.UUID(other_user_id)}
    assert (read_version.await_count > 0) is canonical
    assert [(channel, json.loads(envelope)) for channel, envelope in store.published] == [
        (
            "realtime:ws:instance:remote",
            {
                "message": message,
                "agent_id": agent_id,
                "session_id": session_id,
                "user_id": recipient_user_id,
                "origin_instance_id": "local",
            },
        )
    ]


async def test_send_to_user_explicitly_delivers_across_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    manager = websocket.ConnectionManager()
    router = realtime.RealtimeRouter()
    monkeypatch.setattr(websocket, "realtime_router", router)
    store = MemoryPresence(agent_id, {})

    async def get_redis() -> MemoryPresence:
        return store

    monkeypatch.setattr(realtime, "get_redis", get_redis)
    first_socket, first_frames = await connected_socket()
    second_socket, second_frames = await connected_socket()
    other_user_socket, other_user_frames = await connected_socket()
    manager.active_connections[agent_id] = [
        (first_socket, str(uuid.uuid4()), user_id),
        (second_socket, str(uuid.uuid4()), user_id),
        (other_user_socket, str(uuid.uuid4()), str(uuid.uuid4())),
    ]

    message: websocket.RealtimeMessage = {
        "type": "trigger_notification",
        "content": "Agent update",
        "triggers": ["web_message"],
        "session_id": str(uuid.uuid4()),
    }
    await manager.send_to_user(agent_id, user_id, message)

    assert first_frames == [
        {"type": "websocket.send", "text": json.dumps(message, separators=(",", ":"), ensure_ascii=False)}
    ]
    assert second_frames == first_frames
    assert other_user_frames == []
