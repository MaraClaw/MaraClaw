from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.user import UserRecord
from app.services.channels import inbound
from app.services.channels.submissions import AcceptedChannelEvent, ChannelEvent


@dataclass(slots=True)
class _Handoff:
    agent: AgentRecord
    user: UserRecord
    session: ChatSessionRecord
    operations: list[str] = field(default_factory=list)
    queued: list[dict[str, object]] = field(default_factory=list)
    saved: list[dict[str, object]] = field(default_factory=list)
    accepted: list[tuple[ChannelEvent, str | None, str | None]] = field(default_factory=list)


@pytest.fixture
def handoff(monkeypatch: pytest.MonkeyPatch) -> _Handoff:
    agent = AgentRecord(id=uuid4(), creator_id=uuid4(), name="Bot")
    user = UserRecord(id=uuid4())
    session = ChatSessionRecord(id=uuid4(), agent_id=agent.id, user_id=user.id, source_channel="slack")
    state = _Handoff(agent, user, session)

    async def list_recent(**_values: object) -> list[ChatMessageRecord]:
        state.operations.append("history")
        return []

    async def get_agent(agent_id: UUID) -> AgentRecord:
        assert agent_id == agent.id
        state.operations.append("lookup")
        return agent

    async def persist(**values: object) -> None:
        state.operations.append("persist")
        state.saved.append(values)

    async def enqueue(**values: object) -> GatewayMessageRecord:
        state.operations.append("enqueue")
        state.queued.append(values)
        return GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="queued")

    async def accept(
        event: ChannelEvent, *, content: str | None = None, saved_content: str | None = None
    ) -> AcceptedChannelEvent:
        state.operations.append("accept")
        state.accepted.append((event, content, saved_content))
        return AcceptedChannelEvent(replayed=True)

    monkeypatch.setattr("app.dao.chat_dao.chat_message_dao.list_recent", list_recent)
    monkeypatch.setattr(inbound.agent_dao, "get", get_agent)
    monkeypatch.setattr(inbound, "persist_channel_user_message", persist)
    monkeypatch.setattr(inbound, "accept_channel_event", accept)
    monkeypatch.setattr("app.services.openclaw_routing.enqueue_openclaw_message", enqueue)
    return state


@pytest.mark.parametrize("saved_content", [None, "", "visible text"])
async def test_reply_enqueues_without_reading_history_and_keeps_defaults(
    handoff: _Handoff, saved_content: str | None
) -> None:
    result = await inbound.generate_channel_reply(
        agent_id=handoff.agent.id,
        user_text="runtime text",
        user_id=handoff.user.id,
        session_id=str(handoff.session.id),
        user_message_content=saved_content,
    )
    assert result == inbound.CHANNEL_REPLY_QUEUED
    assert handoff.queued == [
        {
            "agent": handoff.agent,
            "content": "runtime text",
            "sender_user_id": handoff.user.id,
            "conversation_id": str(handoff.session.id),
            "reply_session_id": None,
        }
    ]
    assert handoff.operations == (["lookup", "enqueue"] if saved_content is None else ["lookup", "persist", "enqueue"])


@pytest.mark.parametrize("persist_user", [True, False])
async def test_text_turn_persists_the_current_user_and_enqueues_without_reading_history(
    handoff: _Handoff, persist_user: bool
) -> None:
    reply_session_id = uuid4()
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
    assert handoff.operations == (["persist", "enqueue"] if persist_user else ["enqueue"])
    assert handoff.queued[0]["reply_session_id"] == reply_session_id
    assert handoff.queued[0]["content"] == "runtime text"
    assert "history" not in handoff.queued[0]
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
async def test_native_event_bypasses_legacy_enqueue(handoff: _Handoff, entrypoint: str) -> None:
    event = ChannelEvent(uuid4(), handoff.agent.id, "slack", "native-id", "sender", "thread", "visible text")
    match entrypoint:
        case "reply":
            result = await inbound.generate_channel_reply(
                agent_id=handoff.agent.id,
                user_text="runtime text",
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


async def test_missing_agent_does_not_persist_or_enqueue(handoff: _Handoff, monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing_agent(_agent_id: UUID) -> None:
        return None

    monkeypatch.setattr(inbound.agent_dao, "get", missing_agent)
    result = await inbound.generate_channel_reply(
        agent_id=handoff.agent.id,
        user_text="runtime text",
        user_id=handoff.user.id,
        session_id=str(handoff.session.id),
        user_message_content="visible text",
    )
    assert result == "\u26a0\ufe0f Agent not found."
    assert handoff.operations == []
