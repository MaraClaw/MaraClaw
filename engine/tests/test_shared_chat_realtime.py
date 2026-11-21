import json
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, WebSocket
from starlette.types import Message

from app.api import websocket as ws_api
from app.api.websocket import ConnectionManager, RealtimeMessage, WebSocketConnection
from app.core import access_cache
from app.core.domain.users import UserRole
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.user import UserRecord
from app.services import chat_tickets
from app.services.realtime_runtime import chat_authorization
from app.services.realtime_runtime.router import RealtimeRouter

DONE_TEXT = '{"type":"done"}'


@pytest.fixture(autouse=True)
def connection_scope(monkeypatch):
    """Decisions take a pool checkout; these tests have no pool, and their DAO reads are faked."""
    monkeypatch.setattr(chat_authorization, "connection_ctx", nullcontext)


async def deliver(
    monkeypatch, *, remote, agent_id, session_id, connections, message: RealtimeMessage | None = None, user_id=None
):
    """Deliver one event through the receiver or the publisher, both observed by one manager; return it once every
    close the delivery started has finished."""
    manager = ConnectionManager()
    manager.active_connections[str(agent_id)] = connections
    event: RealtimeMessage = message or {"type": "done"}
    if remote:
        await manager.deliver_pubsub_message(
            agent_id=str(agent_id), session_id=session_id, user_id=user_id, payload=event
        )
    else:
        router = RealtimeRouter()
        monkeypatch.setattr(router, "_list_presence", AsyncMock(return_value=[]))
        await router.route_message(
            agent_id=str(agent_id),
            session_id=session_id,
            user_id=user_id,
            message=event,
            local_connections=connections,
            observer=manager,
        )
    await manager.drain_evictions()
    return manager


def socket_with_binding(agent_id, user_id, session_id, frames, *, authorize_deliveries=True):
    """A connected chat socket whose ticket binding is for ``session_id``; canonical sessions flag theirs."""

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        frames.append(message)

    socket = WebSocket({"type": "websocket", "headers": []}, receive, send)
    socket.state.chat_binding = chat_tickets.ChatTicketBinding(
        user_id=user_id,
        tenant_id=uuid4(),
        agent_id=agent_id,
        session_id=UUID(session_id),
        auth_deadline=int(time.time()) + 3600,
        auth_generation=0,
        authorize_deliveries=authorize_deliveries,
    )
    return socket


def identity_for(agent_id):
    """``authorize_identity`` answering each binding with its own fresh principal."""

    async def authorize(binding):
        return UserRecord(id=binding.user_id), AgentRecord(id=agent_id, name="Agent", creator_id=uuid4()), "use"

    return AsyncMock(side_effect=authorize)


@pytest.mark.parametrize("remote", [False, True])
async def test_revoked_access_closes_the_owner_socket_on_the_next_canonical_event(monkeypatch, remote):
    """S7: the owner's next canonical event re-authorizes the socket, and a revocation closes it with 4003."""
    agent_id, owner = uuid4(), uuid4()
    session_id = str(uuid4())
    frames: list[Message] = []
    socket = socket_with_binding(agent_id, owner, session_id, frames)
    await socket.accept()
    frames.clear()
    authorize = AsyncMock(side_effect=HTTPException(403, "Permission revoked"))
    monkeypatch.setattr(chat_tickets, "authorize_identity", authorize)
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    connections: list[WebSocketConnection] = [(socket, session_id, str(owner))]

    manager = await deliver(
        monkeypatch,
        remote=remote,
        agent_id=agent_id,
        session_id=session_id,
        connections=connections,
        user_id=str(owner),
    )

    # The revoked owner gets no event, only the 4003 close, and leaves the manager.
    assert frames == [{"type": "websocket.close", "code": 4003, "reason": ""}]
    assert manager.active_connections[str(agent_id)] == []
    authorize.assert_awaited_once_with(socket.state.chat_binding)


@pytest.mark.parametrize("remote", [False, True])
async def test_canonical_turn_reaches_only_the_owners_sockets(monkeypatch, remote):
    """S2: user echo, chunk and done reach both of A's tabs; B's own session and an org_admin bound to A's session
    get nothing live."""
    agent_id, owner, other, admin = uuid4(), uuid4(), uuid4(), uuid4()
    owner_session, other_session = str(uuid4()), str(uuid4())
    received: dict[str, list[Message]] = {"a1": [], "a2": [], "b": [], "admin": []}
    a1 = socket_with_binding(agent_id, owner, owner_session, received["a1"])
    a2 = socket_with_binding(agent_id, owner, owner_session, received["a2"])
    b = socket_with_binding(agent_id, other, other_session, received["b"])
    # Its ticket was issued for A's canonical session, so its binding is flagged as well.
    viewer = socket_with_binding(agent_id, admin, owner_session, received["admin"])
    for socket in (a1, a2, b, viewer):
        await socket.accept()
    for frames in received.values():
        frames.clear()
    authorize = identity_for(agent_id)
    read_version = AsyncMock(return_value="1")
    monkeypatch.setattr(chat_tickets, "authorize_identity", authorize)
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    monkeypatch.setattr(access_cache, "read_acl_version", read_version)
    manager = ConnectionManager()
    manager.active_connections[str(agent_id)] = [
        (a1, owner_session, str(owner)),
        (a2, owner_session, str(owner)),
        (b, other_session, str(other)),
        (viewer, owner_session, str(admin)),
    ]
    monkeypatch.setattr(ws_api.realtime_router, "_list_presence", AsyncMock(return_value=[]))
    turn_id = str(uuid4())
    events: list[RealtimeMessage] = [
        {"type": "done", "role": "user", "content": "hello", "message_id": str(uuid4()), "session_id": owner_session},
        {"type": "chunk", "content": "Hi", "turn_id": turn_id, "seq": 0, "replace": False},
        {"type": "done", "role": "assistant", "content": "Hi", "turn_id": turn_id, "session_id": owner_session},
    ]

    for event in events:
        if remote:
            await manager.deliver_pubsub_message(
                agent_id=str(agent_id), session_id=owner_session, user_id=str(owner), payload=event
            )
        else:
            _ = await manager.send_to_session(str(agent_id), owner_session, event, user_id=str(owner))
    await manager.drain_evictions()

    for name in ("a1", "a2"):
        assert [json.loads(frame["text"])["type"] for frame in received[name]] == ["done", "chunk", "done"]
        assert json.loads(received[name][0]["text"])["role"] == "user"
    assert received["b"] == []
    assert received["admin"] == []
    # Both owner tabs share one decision; the ACL version is read once per event, before any socket authorizes.
    assert authorize.await_count == 2
    assert {call.args[0].user_id for call in authorize.await_args_list} == {owner}
    assert read_version.await_count == len(events)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("binding_state", ["unflagged", "missing", "other_session", "other_agent"])
async def test_a_socket_without_a_matching_flagged_binding_is_delivered_without_any_work(
    monkeypatch, remote, binding_state
):
    agent_id, user_id = uuid4(), uuid4()
    session_id = str(uuid4())
    frames: list[Message] = []
    socket = socket_with_binding(
        agent_id, user_id, session_id, frames, authorize_deliveries=binding_state != "unflagged"
    )
    await socket.accept()
    frames.clear()
    if binding_state == "missing":
        del socket.state.chat_binding
    elif binding_state == "other_session":
        socket.state.chat_binding.session_id = uuid4()
    elif binding_state == "other_agent":
        socket.state.chat_binding.agent_id = uuid4()
    authorize = AsyncMock()
    read_version = AsyncMock(return_value="1")
    monkeypatch.setattr(chat_tickets, "authorize_identity", authorize)
    monkeypatch.setattr(access_cache, "read_acl_version", read_version)
    connections: list[WebSocketConnection] = [(socket, session_id, str(user_id))]

    _ = await deliver(monkeypatch, remote=remote, agent_id=agent_id, session_id=session_id, connections=connections)

    assert frames == [{"type": "websocket.send", "text": DONE_TEXT}]
    authorize.assert_not_awaited()
    read_version.assert_not_awaited()


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize(
    ("privilege", "allowed"),
    [("owner", True), ("org_admin", True), ("member", False), ("revoked_owner", False)],
)
async def test_canonical_delivery_uses_fresh_principal_and_session(monkeypatch, remote, privilege, allowed):
    agent_id, user_id = uuid4(), uuid4()
    session_id = str(uuid4())
    frames: list[Message] = []
    socket = socket_with_binding(agent_id, user_id, session_id, frames)
    await socket.accept()
    frames.clear()
    user = UserRecord(id=user_id, role=UserRole.ORG_ADMIN if privilege == "org_admin" else UserRole.MEMBER)
    agent = AgentRecord(id=agent_id, name="Agent", creator_id=uuid4())
    session = ChatSessionRecord(
        id=UUID(session_id),
        agent_id=agent_id,
        user_id=user_id if privilege in {"owner", "revoked_owner"} else uuid4(),
        source_channel="web",
        is_primary=True,
    )
    authorize = (
        AsyncMock(side_effect=HTTPException(403, "Agent access revoked"))
        if privilege == "revoked_owner"
        else AsyncMock(return_value=(user, agent, "use"))
    )
    monkeypatch.setattr(chat_tickets, "authorize_identity", authorize)
    monkeypatch.setattr(chat_tickets.chat_session_dao, "get_for_agent", AsyncMock(return_value=session))
    connections: list[WebSocketConnection] = [(socket, session_id, str(user_id))]

    _ = await deliver(monkeypatch, remote=remote, agent_id=agent_id, session_id=session_id, connections=connections)

    assert frames == (
        [{"type": "websocket.send", "text": DONE_TEXT}]
        if allowed
        else [{"type": "websocket.close", "code": 4003, "reason": ""}]
    )


async def test_canonical_route_keeps_the_owner_filter(monkeypatch):
    agent_id, owner = uuid4(), uuid4()
    session_id = str(uuid4())
    route = AsyncMock()
    monkeypatch.setattr(ws_api.realtime_router, "route_message", route)

    _ = await ConnectionManager().send_to_session(str(agent_id), session_id, {"type": "done"}, user_id=str(owner))

    assert route.await_args is not None
    assert route.await_args.kwargs["session_id"] == session_id
    assert route.await_args.kwargs["user_id"] == str(owner)


async def test_canonical_redis_envelope_targets_only_the_owners_instances(monkeypatch):
    agent_id, owner = str(uuid4()), str(uuid4())
    session_id = str(uuid4())
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
                {"instance_id": "owner-remote", "session_id": session_id, "user_id": owner},
                {"instance_id": "admin-remote", "session_id": session_id, "user_id": str(uuid4())},
                {"instance_id": "private-remote", "session_id": str(uuid4()), "user_id": owner},
            ]
        ),
    )

    _ = await router.route_message(
        agent_id=agent_id, session_id=session_id, user_id=owner, message={"type": "done"}, local_connections=[]
    )

    publish.assert_awaited_once()
    assert publish.await_args is not None
    channel, envelope = publish.await_args.args
    assert channel == "realtime:ws:instance:owner-remote"
    assert json.loads(envelope)["session_id"] == session_id
    assert json.loads(envelope)["user_id"] == owner


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("flagged", [False, True])
async def test_acl_version_is_read_once_per_delivery_and_only_for_flagged_sockets(monkeypatch, remote, flagged):
    agent_id, user_id = uuid4(), uuid4()
    session_id = str(uuid4())
    frames: list[Message] = []
    sockets = [
        socket_with_binding(agent_id, user_id, session_id, frames, authorize_deliveries=flagged) for _ in range(5)
    ]
    for socket in sockets:
        await socket.accept()
    frames.clear()
    read_version = AsyncMock(return_value="7")
    authorize = identity_for(agent_id)
    monkeypatch.setattr(access_cache, "read_acl_version", read_version)
    monkeypatch.setattr(chat_tickets, "authorize_identity", authorize)
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    connections: list[WebSocketConnection] = [(socket, session_id, str(user_id)) for socket in sockets]

    _ = await deliver(monkeypatch, remote=remote, agent_id=agent_id, session_id=session_id, connections=connections)

    assert len(frames) == len(sockets)
    assert read_version.await_count == (1 if flagged else 0)
    # Each socket's ticket names its own tenant, so each is its own identity and decision.
    assert authorize.await_count == (len(sockets) if flagged else 0)
