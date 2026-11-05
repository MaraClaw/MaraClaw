import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.services.trigger_runtime.dispatch import runtime_execution_payload
from app.services.trigger_runtime.evaluator import check_new_agent_messages, is_private_url


def test_is_private_url_blocks_unspecified_ipv4():
    # Given
    url = "http://0.0.0.0:8080"

    # When
    result = is_private_url(url)

    # Then
    assert result is True


TENANT = uuid.uuid4()
FOREIGN_TENANT = uuid.uuid4()
MESSAGE_ID = uuid.uuid4()


class _FakeDb:
    """Answers the agent-name lookup from an in-memory agent table and records every statement."""

    def __init__(self, agents: list[dict[str, Any]], tenants: dict[uuid.UUID, uuid.UUID]) -> None:
        self.agents = agents
        self.tenants = tenants
        self.statements: list[tuple[str, dict[str, Any]]] = []

    async def fetchone(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        normalized = " ".join(sql.split())
        assert params is not None
        self.statements.append((normalized, params))
        if normalized.startswith("SELECT a.id FROM agents a"):
            assert "JOIN agents ta ON ta.id = %(trigger_agent_id)s" in normalized
            assert "AND a.tenant_id = ta.tenant_id" in normalized
            assert set(params) == {"pattern", "trigger_agent_id"}
            tenant = self.tenants[params["trigger_agent_id"]]
            needle = params["pattern"].strip("%").lower()
            for agent in self.agents:
                if needle in agent["name"].lower() and agent["tenant_id"] == tenant:
                    return {"id": agent["id"]}
            return None
        assert normalized.startswith("SELECT cm.id, cm.content FROM chat_messages cm")
        assert "AND cs.source_channel <> 'trigger'" in normalized
        assert set(params) == {"participant_id", "since", "roles"}
        return {"id": MESSAGE_ID, "content": "Designed the logo"}


def _trigger(agent_id: uuid.UUID, name: str = "Ray") -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="test_trigger",
        type="on_message",
        config={"from_agent_name": name},
        is_enabled=True,
        created_at=datetime.now(UTC),
        fire_count=0,
        last_fired_at=None,
    )


async def _check(trigger: Any, db: _FakeDb) -> tuple[str | None, AsyncMock]:
    @asynccontextmanager
    async def fake_connection_ctx():
        yield db

    participant = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    with (
        patch("app.services.trigger_runtime.evaluator.connection_ctx", fake_connection_ctx),
        patch("app.services.trigger_runtime.evaluator.participant_dao.get_by_type_ref", participant),
    ):
        return await check_new_agent_messages(trigger), participant


@pytest.mark.asyncio
async def test_check_new_agent_messages_matches_user_role_and_returns_the_message_id():
    """Verify check_new_agent_messages matches messages from an agent and returns the chat message id."""
    agent_id, source_agent_id = uuid.uuid4(), uuid.uuid4()
    trigger = _trigger(agent_id)
    db = _FakeDb([{"id": source_agent_id, "name": "Ray", "tenant_id": TENANT}], {agent_id: TENANT})

    result, participant = await _check(trigger, db)

    assert result == str(MESSAGE_ID)
    participant.assert_awaited_once_with("agent", source_agent_id)
    assert trigger.config["_matched_message"] == "Designed the logo"
    assert trigger.config["_matched_message_id"] == str(MESSAGE_ID)
    assert trigger.config["_matched_from"] == "Ray"
    # The id travels into the queued execution
    assert runtime_execution_payload(trigger)["_matched_message_id"] == str(MESSAGE_ID)


@pytest.mark.asyncio
async def test_agent_name_in_another_tenant_is_not_resolved():
    agent_id = uuid.uuid4()
    trigger = _trigger(agent_id)
    db = _FakeDb([{"id": uuid.uuid4(), "name": "Ray", "tenant_id": FOREIGN_TENANT}], {agent_id: TENANT})

    result, participant = await _check(trigger, db)

    assert result is None
    participant.assert_not_awaited()
    assert [sql.split(" FROM ")[0] for sql, _ in db.statements] == ["SELECT a.id"]
    assert "_matched_message_id" not in trigger.config


@pytest.mark.asyncio
async def test_same_named_agent_in_own_tenant_wins_over_another_tenant():
    agent_id, own_agent_id = uuid.uuid4(), uuid.uuid4()
    trigger = _trigger(agent_id)
    db = _FakeDb(
        [
            {"id": uuid.uuid4(), "name": "Ray", "tenant_id": FOREIGN_TENANT},
            {"id": own_agent_id, "name": "Ray", "tenant_id": TENANT},
        ],
        {agent_id: TENANT},
    )

    result, participant = await _check(trigger, db)

    assert result == str(MESSAGE_ID)
    participant.assert_awaited_once_with("agent", own_agent_id)
