from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.whatsapp_personal import PersonalSource
from app.services.channels import submissions
from app.services.channels.submissions import ChannelEvent


class TransactionOpenedError(Exception):
    pass


@pytest.fixture
def order(monkeypatch: pytest.MonkeyPatch) -> tuple[AgentRecord, list[tuple[str, object]], AsyncMock]:
    """Record whether the owner's canonical session is created before the acceptance transaction opens."""
    agent = AgentRecord(id=uuid4(), creator_id=uuid4(), tenant_id=uuid4(), name="Bot")
    events: list[tuple[str, object]] = []

    async def ensure(agent_id, user_id):
        events.append(("ensure", (agent_id, user_id)))

    class OpensTransaction:
        async def __aenter__(self):
            events.append(("transaction", None))
            raise TransactionOpenedError

        async def __aexit__(self, *_exc):
            return None

    ensure_mock = AsyncMock(side_effect=ensure)
    monkeypatch.setattr(submissions.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(submissions, "ensure_canonical_session", ensure_mock)
    monkeypatch.setattr(submissions, "connection_ctx", OpensTransaction)
    return agent, events, ensure_mock


def event_for(agent: AgentRecord, *, personal: bool) -> ChannelEvent:
    source = PersonalSource(uuid4(), uuid4(), uuid4(), uuid4(), "account", "native") if personal else None
    return ChannelEvent(
        config_id=uuid4(),
        agent_id=agent.id,
        provider="whatsapp",
        event_key="native",
        sender_id="account",
        conversation_id="self",
        text="hello",
        personal_source=source,
    )


async def test_personal_event_creates_the_creators_canonical_session_before_the_transaction(order):
    # Given a Personal event, whose sender is the agent creator and whose transaction locks tenant, user and agent.
    agent, events, _ = order
    # When it is accepted.
    with pytest.raises(TransactionOpenedError):
        await submissions.accept_channel_event(event_for(agent, personal=True))
    # Then the canonical session is ensured for the creator first, outside that transaction.
    assert events == [("ensure", (agent.id, agent.creator_id)), ("transaction", None)]


async def test_direct_provider_event_leaves_session_creation_to_binding(order):
    # Given an ordinary provider event, which only holds share-mode locks until binding.
    agent, events, ensure = order
    # When it is accepted.
    with pytest.raises(TransactionOpenedError):
        await submissions.accept_channel_event(event_for(agent, personal=False))
    # Then nothing is created ahead of the transaction.
    assert events == [("transaction", None)]
    ensure.assert_not_awaited()
