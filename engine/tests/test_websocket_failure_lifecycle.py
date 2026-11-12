"""Connection ownership and error reporting must survive unavailable transports."""

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Literal, assert_never
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, WebSocket
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.types import Message

from app.api import websocket
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord
from app.records.user import UserRecord
from app.services import chat_tickets
from app.services.chat_submissions import AcceptedSubmission
from app.services.realtime_runtime import router as realtime

type Lifecycle = tuple[chat_tickets.ChatTicketBinding, websocket.ConnectionManager, AsyncMock]


@pytest.fixture
async def lifecycle(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Lifecycle]:
    binding = chat_tickets.ChatTicketBinding(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        auth_deadline=int(time.time()) + 3600,
        auth_generation=7,
    )
    user = UserRecord(id=binding.user_id, tenant_id=binding.tenant_id, display_name="Member")
    agent = AgentRecord(id=binding.agent_id, creator_id=user.id, name="Agent")
    manager = websocket.ConnectionManager()
    execute = AsyncMock(return_value=[])
    monkeypatch.setattr(websocket, "manager", manager)
    monkeypatch.setattr(websocket, "realtime_router", realtime.RealtimeRouter())
    monkeypatch.setattr(chat_tickets, "redeem", AsyncMock(return_value=binding))
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(return_value=(user, agent, "use")))
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    monkeypatch.setattr(websocket.chat_message_dao, "list_recent", AsyncMock(return_value=[]))
    # Keep Redis's real pipeline and command queue; replace only the network execution.
    monkeypatch.setattr(Pipeline, "execute", execute)
    async with Redis() as redis:
        monkeypatch.setattr(realtime, "get_redis", AsyncMock(return_value=redis))
        yield binding, manager, execute


def peer_socket(sent: list[Message], *, broken_send: bool = False, frames: tuple[Message, ...] = ()) -> WebSocket:
    incoming: list[Message] = [
        {"type": "websocket.connect"},
        *frames,
        {"type": "websocket.disconnect", "code": 1000},
    ]
    messages = iter(incoming)

    async def receive() -> Message:
        return next(messages)

    async def send(message: Message) -> None:
        sent.append(message)
        if broken_send and message["type"] != "websocket.accept":
            # Starlette, not this test, converts this ASGI failure to disconnect 1006.
            raise OSError("peer transport is already closed")

    return WebSocket({"type": "websocket", "headers": []}, receive, send)


@pytest.mark.parametrize("failure", ["http-error", "auth-timeout", "redis-timeout"])
async def test_peer_disconnect_during_error_reporting_is_contained(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["http-error", "auth-timeout", "redis-timeout"],
) -> None:
    # Given a real socket whose transport fails only after the handshake.
    binding, manager, execute = lifecycle
    sent: list[Message] = []
    ws = peer_socket(sent, broken_send=True)
    match failure:
        case "http-error":
            monkeypatch.setattr(chat_tickets, "redeem", AsyncMock(side_effect=HTTPException(503, "unavailable")))
            expected_frame = "websocket.send"
            expected_code = None
        case "auth-timeout":
            monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(side_effect=TimeoutError("auth deadline")))
            expected_frame = "websocket.close"
            expected_code = 4001
        case "redis-timeout":
            execute.side_effect = RedisTimeoutError("presence registration timeout")
            expected_frame = "websocket.close"
            expected_code = 1011
        case unreachable:
            assert_never(unreachable)
    handler = websocket.WebSocketChatHandler(ws, binding.agent_id, "opaque", str(binding.session_id))

    # When reporting the original error encounters Starlette's disconnect 1006.
    await handler.run()

    # Then the secondary transport failure is contained and local ownership is released.
    assert len(sent) == 2
    assert sent[-1]["type"] == expected_frame
    assert sent[-1].get("code") == expected_code
    assert manager.active_connections.get(str(binding.agent_id), []) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("history_failed", [False, True])
async def test_welcome_requires_successfully_loaded_empty_history(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, history_failed: bool
) -> None:
    binding, _manager, _execute = lifecycle
    user = UserRecord(id=binding.user_id, tenant_id=binding.tenant_id)
    agent = AgentRecord(id=binding.agent_id, creator_id=user.id, name="Agent", welcome_message="Welcome")
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(return_value=(user, agent, "use")))
    history = AsyncMock(return_value=[], side_effect=RuntimeError("history unavailable") if history_failed else None)
    monkeypatch.setattr(websocket.chat_message_dao, "list_recent", history)
    report = Mock()
    monkeypatch.setattr(websocket, "report_exception", report)
    sent: list[Message] = []
    handler = websocket.WebSocketChatHandler(peer_socket(sent), binding.agent_id, "opaque", str(binding.session_id))

    await handler.run()

    events = [json.loads(frame["text"]) for frame in sent if frame["type"] == "websocket.send"]
    assert [event["type"] for event in events] == (["connected"] if history_failed else ["connected", "done"])
    assert not any(frame["type"] == "websocket.close" for frame in sent)
    assert report.call_count == int(history_failed)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["not json", "[]", "null", '"text"', "42", b"binary"])
async def test_malformed_frame_reports_error_and_next_submission_is_processed(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, payload: str | bytes
) -> None:
    binding, _manager, _execute = lifecycle
    submission_id = str(uuid.uuid4())
    message = ChatMessageRecord(
        id=uuid.uuid4(), agent_id=binding.agent_id, user_id=binding.user_id, role="user", content="hello"
    )
    accept = AsyncMock(return_value=AcceptedSubmission(message=message, replayed=True))
    monkeypatch.setattr(websocket, "accept_submission", accept)
    report = Mock()
    monkeypatch.setattr(websocket, "report_exception", report)
    malformed: Message = {"type": "websocket.receive"}
    malformed["bytes" if isinstance(payload, bytes) else "text"] = payload
    valid: Message = {
        "type": "websocket.receive",
        "text": json.dumps({"content": "hello", "client_submission_id": submission_id}),
    }
    sent: list[Message] = []
    handler = websocket.WebSocketChatHandler(
        peer_socket(sent, frames=(malformed, valid)), binding.agent_id, "opaque", str(binding.session_id)
    )

    await handler.run()

    events = [json.loads(frame["text"]) for frame in sent if frame["type"] == "websocket.send"]
    assert [event["type"] for event in events] == ["connected", "error", "turn_accepted"]
    assert isinstance(events[1]["content"], str)
    assert events[2]["client_submission_id"] == submission_id
    accept.assert_awaited_once()
    assert accept.call_args.args[0].content == "hello"
    assert not any(frame["type"] == "websocket.close" for frame in sent)
    report.assert_not_called()


async def test_redis_unregister_failure_is_contained_after_peer_disconnect(lifecycle: Lifecycle) -> None:
    # Given successful presence registration followed by a Redis failure during cleanup.
    binding, manager, execute = lifecycle
    execute.side_effect = [[], RedisTimeoutError("presence unregister timeout")]
    sent: list[Message] = []
    ws = peer_socket(sent)
    handler = websocket.WebSocketChatHandler(ws, binding.agent_id, "opaque", str(binding.session_id))

    # When the peer disconnects normally, the real manager and router perform cleanup.
    await handler.run()

    # Then Redis's secondary failure does not escape the socket lifecycle.
    assert execute.await_count == 2
    assert manager.active_connections.get(str(binding.agent_id), []) == []
    assert isinstance(sent[1]["text"], str)
    assert json.loads(sent[1]["text"])["type"] == "connected"


async def test_failed_redis_registration_never_publishes_local_socket(lifecycle: Lifecycle) -> None:
    # Given a real manager whose Redis registration pipeline times out.
    binding, manager, execute = lifecycle
    execute.side_effect = RedisTimeoutError("presence registration timeout")
    sent: list[Message] = []
    ws = peer_socket(sent)

    # When registration fails, the caller still receives the original Redis failure.
    with pytest.raises(RedisTimeoutError, match="presence registration timeout"):
        await manager.connect(str(binding.agent_id), ws, str(binding.session_id), str(binding.user_id))

    # Then an unregistered socket cannot be discovered by local delivery.
    assert manager.active_connections.get(str(binding.agent_id), []) == []


async def test_healthy_registration_and_peer_disconnect_release_local_socket(lifecycle: Lifecycle) -> None:
    # Given healthy Redis presence and a peer that disconnects after setup.
    binding, manager, execute = lifecycle
    sent: list[Message] = []
    ws = peer_socket(sent)
    handler = websocket.WebSocketChatHandler(ws, binding.agent_id, "opaque", str(binding.session_id))

    # When the real lifecycle runs through setup and peer disconnect.
    await handler.run()

    # Then the connected event and both Redis operations survive unchanged.
    assert sent[0]["type"] == "websocket.accept"
    assert isinstance(sent[1]["text"], str)
    assert json.loads(sent[1]["text"]) == {"type": "connected", "session_id": str(binding.session_id)}
    assert len(sent) == 2
    assert execute.await_count == 2
    assert manager.active_connections.get(str(binding.agent_id), []) == []
