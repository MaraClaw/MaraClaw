from __future__ import annotations

from dataclasses import dataclass, field
from typing import assert_type
from uuid import UUID, uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.user import UserRecord
from app.services.channels import inbound
from app.services.channels.submissions import AcceptedChannelEvent, ChannelEvent
from app.services.llm.types import OpenAIMessage


@dataclass(slots=True)
class _HistoryHandoff:
    agent: AgentRecord
    user: UserRecord
    session: ChatSessionRecord
    rows: list[ChatMessageRecord] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    queries: list[tuple[UUID, str | None, int]] = field(default_factory=list)
    queued: list[dict[str, object]] = field(default_factory=list)
    saved: list[dict[str, object]] = field(default_factory=list)
    accepted: list[tuple[ChannelEvent, str | None, str | None]] = field(default_factory=list)


@pytest.fixture
def handoff(monkeypatch: pytest.MonkeyPatch) -> _HistoryHandoff:
    agent = AgentRecord(id=uuid4(), creator_id=uuid4(), name="Bot")
    user = UserRecord(id=uuid4())
    session = ChatSessionRecord(id=uuid4(), agent_id=agent.id, user_id=user.id, source_channel="slack")
    state = _HistoryHandoff(agent, user, session)

    async def list_recent(
        *, agent_id: UUID, conversation_id: str | None = None, limit: int = 50
    ) -> list[ChatMessageRecord]:
        state.operations.append("history")
        state.queries.append((agent_id, conversation_id, limit))
        return state.rows[-limit:]

    async def get_agent(agent_id: UUID) -> AgentRecord:
        assert agent_id == agent.id
        state.operations.append("lookup")
        return agent

    async def persist(**values: object) -> None:
        state.operations.append("persist")
        state.saved.append(values)

    async def enqueue(*, history: list[OpenAIMessage] | None, **values: object) -> GatewayMessageRecord:
        state.operations.append("enqueue")
        state.queued.append(values | {"history": history})
        return GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="queued")

    async def accept(
        event: ChannelEvent, *, content: str | None = None, saved_content: str | None = None
    ) -> AcceptedChannelEvent:
        state.operations.append("accept")
        state.accepted.append((event, content, saved_content))
        return AcceptedChannelEvent(replayed=True)

    monkeypatch.setattr(inbound.chat_message_dao, "list_recent", list_recent)
    monkeypatch.setattr(inbound.agent_dao, "get", get_agent)
    monkeypatch.setattr(inbound, "persist_channel_user_message", persist)
    monkeypatch.setattr(inbound, "accept_channel_event", accept)
    monkeypatch.setattr("app.services.openclaw_routing.enqueue_openclaw_message", enqueue)
    return state


@pytest.mark.parametrize(("window", "limit"), [(None, 8), (0, 8), (-1, 8), (100, 8), (3, 3), (1, 1)])
async def test_history_loading_preserves_converted_order_and_window(
    handoff: _HistoryHandoff, window: int | None, limit: int
) -> None:
    tool_id = uuid4()
    handoff.rows.extend(
        [
            ChatMessageRecord(uuid4(), handoff.agent.id, handoff.user.id, "user", "earlier"),
            ChatMessageRecord(uuid4(), handoff.agent.id, handoff.user.id, "assistant", "answer", thinking="reasoning"),
            ChatMessageRecord(
                tool_id,
                handoff.agent.id,
                handoff.user.id,
                "tool_call",
                '{"name":"lookup","args":{"key":"value"},"result":"found"}',
            ),
        ]
    )
    expected: list[OpenAIMessage] = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "answer", "reasoning_content": "reasoning"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"call_{tool_id}",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"key": "value"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": f"call_{tool_id}", "content": "found"},
    ]
    history = await inbound.load_history_for_session(
        agent_id=handoff.agent.id, session=handoff.session, context_window_size=window
    )
    assert_type(history, list[OpenAIMessage])
    assert history == (expected[-2:] if limit == 1 else expected)
    assert handoff.queries == [(handoff.agent.id, str(handoff.session.id), limit)]


@pytest.mark.parametrize("history_size", [0, 3, 12])
@pytest.mark.parametrize("saved_content", [None, "", "visible text"])
async def test_reply_bounds_history_without_mutation_and_keeps_defaults(
    handoff: _HistoryHandoff, history_size: int, saved_content: str | None
) -> None:
    history: list[OpenAIMessage] = [{"role": "user", "content": str(index)} for index in range(history_size)]
    original = history.copy()
    result = await inbound.generate_channel_reply(
        agent_id=handoff.agent.id,
        user_text="runtime text",
        history=history,
        user_id=handoff.user.id,
        session_id=str(handoff.session.id),
        user_message_content=saved_content,
    )
    assert result == inbound.CHANNEL_REPLY_QUEUED
    assert history == original
    assert handoff.queued == [
        {
            "agent": handoff.agent,
            "content": "runtime text",
            "sender_user_id": handoff.user.id,
            "conversation_id": str(handoff.session.id),
            "reply_session_id": None,
            "history": original[-8:] if original else None,
            "await_wake": False,
        }
    ]
    assert handoff.operations == (["lookup", "enqueue"] if saved_content is None else ["lookup", "persist", "enqueue"])


@pytest.mark.parametrize("persist_user", [True, False])
async def test_text_turn_loads_prior_history_before_persisting_current_user(
    handoff: _HistoryHandoff, persist_user: bool
) -> None:
    reply_session_id = uuid4()
    handoff.rows.extend(
        ChatMessageRecord(uuid4(), handoff.agent.id, handoff.user.id, "user", str(index)) for index in range(12)
    )
    result = await inbound.run_text_turn(
        agent=handoff.agent,
        agent_id=handoff.agent.id,
        platform_user=handoff.user,
        session=handoff.session,
        user_text="visible text",
        llm_user_text="runtime text",
        reply_session_id=reply_session_id,
        agent_model=handoff.agent,
        persist_user=persist_user,
    )
    assert result == inbound.CHANNEL_REPLY_QUEUED
    assert handoff.operations == (["history", "persist", "enqueue"] if persist_user else ["history", "enqueue"])
    assert handoff.queued[0]["history"] == [{"role": "user", "content": str(index)} for index in range(4, 12)]
    assert handoff.queued[0]["reply_session_id"] == reply_session_id
    assert handoff.queued[0]["content"] == "runtime text"
    assert handoff.saved == (
        [
            {
                "agent_id": handoff.agent.id,
                "user_id": handoff.user.id,
                "conversation_id": str(handoff.session.id),
                "content": "visible text",
            }
        ]
        if persist_user
        else []
    )


@pytest.mark.parametrize("entrypoint", ["reply", "turn"])
async def test_native_event_bypasses_legacy_history_and_enqueue(handoff: _HistoryHandoff, entrypoint: str) -> None:
    event = ChannelEvent(uuid4(), handoff.agent.id, "slack", "native-id", "sender", "thread", "visible text")
    match entrypoint:
        case "reply":
            result = await inbound.generate_channel_reply(
                agent_id=handoff.agent.id,
                user_text="runtime text",
                history=[],
                user_id=handoff.user.id,
                session_id=str(handoff.session.id),
                user_message_content="visible text",
                native_event=event,
            )
        case "turn":
            result = await inbound.run_text_turn(
                agent=handoff.agent,
                agent_id=handoff.agent.id,
                platform_user=handoff.user,
                session=handoff.session,
                user_text="visible text",
                llm_user_text="runtime text",
                native_event=event,
            )
        case _:
            raise AssertionError(entrypoint)
    assert result == inbound.CHANNEL_REPLY_QUEUED
    assert handoff.operations == ["accept"]
    assert handoff.accepted == [(event, "runtime text", "visible text")]


async def test_missing_agent_does_not_persist_or_enqueue(
    handoff: _HistoryHandoff, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing_agent(_agent_id: UUID) -> None:
        return None

    monkeypatch.setattr(inbound.agent_dao, "get", missing_agent)
    result = await inbound.generate_channel_reply(
        agent_id=handoff.agent.id,
        user_text="runtime text",
        history=[],
        user_id=handoff.user.id,
        session_id=str(handoff.session.id),
        user_message_content="visible text",
    )
    assert result == "\u26a0\ufe0f Agent not found."
    assert handoff.operations == []
