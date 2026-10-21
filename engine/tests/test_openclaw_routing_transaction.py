import asyncio
from collections.abc import AsyncIterator, Mapping
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.api import background_tasks
from app.db import session
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_routing as routing
from test_db_session import _FakePool, _FakeRawConnection


@pytest.fixture
async def raw(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_FakeRawConnection]:
    connection = _FakeRawConnection()
    monkeypatch.setattr(session, "get_pool", lambda: _FakePool(connection))
    token = session._conn_ctx.set(None)
    background_tasks.start_background_tasks()
    try:
        yield connection
    finally:
        await background_tasks.drain_background_tasks(grace_seconds=0)
        background_tasks.start_background_tasks()
        session._conn_ctx.reset(token)


@pytest.mark.parametrize("rollback", [False, True])
async def test_prepared_enqueue_defers_wake_until_commit(
    monkeypatch: pytest.MonkeyPatch, raw: _FakeRawConnection, rollback: bool
) -> None:
    # Given
    agent = AgentRecord(id=uuid4(), name="Destination", creator_id=uuid4())
    sender = uuid4()
    prepared = routing.PreparedOpenClawMessage(agent, "relay payload", "primary", "openai/model", "complex", "rule")
    created = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content=prepared.content)
    inserted: list[Mapping[str, object]] = []
    woke = asyncio.Event()
    wake_context: list[tuple[int, object]] = []

    async def create(*, obj_in: Mapping[str, object]) -> GatewayMessageRecord:
        assert session.get_connection() is not None
        inserted.append(obj_in)
        return created

    async def wake(agent: AgentRecord, content: str, message_id: object) -> None:
        assert (agent.id, content, message_id) == (prepared.agent.id, prepared.content, created.id)
        wake_context.append((raw.commits, session.get_connection()))
        woke.set()

    monkeypatch.setattr(routing.gateway_message_dao, "create", create)
    monkeypatch.setattr(routing, "_run_inbox_wake", wake)
    for name in ("ensure_agent_company_models", "load_agent_model_bundle", "select_turn_model"):
        monkeypatch.setattr(routing, name, AsyncMock(side_effect=AssertionError("preparation inside transaction")))
    monkeypatch.setattr(routing.agent_manager, "write_guest_config", Mock(side_effect=AssertionError("config in tx")))

    async def enqueue() -> GatewayMessageRecord:
        async with session.connection_ctx():
            result = await routing.enqueue_prepared_openclaw_message(
                prepared, sender_agent_id=sender, conversation_id="relay-intent"
            )
            await asyncio.sleep(0)
            assert not woke.is_set()
            if rollback:
                raise RuntimeError("rollback")
            return result

    # When
    if rollback:
        with pytest.raises(RuntimeError, match="rollback"):
            await enqueue()
        await background_tasks.drain_background_tasks()
    else:
        assert await enqueue() is created
        await asyncio.wait_for(woke.wait(), timeout=1)
        await asyncio.gather(*routing._wake_tasks)
    # Then
    assert inserted == [
        {
            "agent_id": agent.id,
            "sender_user_id": None,
            "sender_agent_id": sender,
            "conversation_id": "relay-intent",
            "content": "relay payload",
            "status": "pending",
            "selected_slot": "primary",
            "guest_model_ref": "openai/model",
            "complexity": "complex",
            "routing_reason": "rule",
        }
    ]
    assert wake_context == ([] if rollback else [(1, None)])
    assert raw.rollbacks == int(rollback)


async def test_prepared_enqueue_suppresses_duplicate_user_message(
    monkeypatch: pytest.MonkeyPatch, raw: _FakeRawConnection
) -> None:
    # Given
    agent = AgentRecord(id=uuid4(), name="Destination", creator_id=uuid4())
    sender = uuid4()
    prepared = routing.PreparedOpenClawMessage(agent, "hello", "secondary", "openai/cheap", "manageable", "rule")
    pending = GatewayMessageRecord(
        id=uuid4(), agent_id=agent.id, content="hello", sender_user_id=sender, conversation_id="thread"
    )
    monkeypatch.setattr(routing.gateway_message_dao, "list_pending", AsyncMock(return_value=[pending]))
    create = AsyncMock(side_effect=AssertionError("duplicate insert"))
    monkeypatch.setattr(routing.gateway_message_dao, "create", create)
    scheduled = Mock()
    monkeypatch.setattr(routing, "_schedule_inbox_wake", scheduled)
    # When
    result = await routing.enqueue_prepared_openclaw_message(prepared, sender_user_id=sender, conversation_id="thread")
    # Then
    assert result is pending
    create.assert_not_awaited()
    scheduled.assert_called_once_with(agent, "hello", pending.id)
