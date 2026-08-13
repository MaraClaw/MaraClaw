import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.trigger_runtime.evaluator import check_new_agent_messages, is_private_url


def test_is_private_url_blocks_unspecified_ipv4():
    # Given
    url = "http://0.0.0.0:8080"

    # When
    result = is_private_url(url)

    # Then
    assert result is True


@pytest.mark.asyncio
async def test_check_new_agent_messages_matches_user_role():
    """Verify check_new_agent_messages matches messages from agent with role='user'."""
    agent_id = uuid.uuid4()
    source_agent_id = uuid.uuid4()
    participant_id = uuid.uuid4()

    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="test_trigger",
        type="on_message",
        config={"from_agent_name": "Ray"},
        is_enabled=True,
        created_at=datetime.now(UTC),
        fire_count=0,
        last_fired_at=None,
    )

    fetchone_results = [
        {"id": source_agent_id},  # agents name ILIKE lookup
        {"content": "Designed the logo"},  # chat_messages join lookup
    ]

    class FakeDb:
        async def fetchone(self, _sql, _params=None):
            if not fetchone_results:
                raise AssertionError("unexpected fetchone() call")
            return fetchone_results.pop(0)

        async def execute(self, _sql, _params=None):
            raise AssertionError("unexpected execute() call")

        async def commit(self):
            return None

        async def rollback(self):
            return None

    @asynccontextmanager
    async def fake_connection_ctx():
        yield FakeDb()

    with (
        patch("app.services.trigger_runtime.evaluator.connection_ctx", fake_connection_ctx),
        patch(
            "app.services.trigger_runtime.evaluator.participant_dao.get_by_type_ref",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(id=participant_id),
        ),
    ):
        result = await check_new_agent_messages(trigger)

    assert result is True
    assert trigger.config["_matched_message"] == "Designed the logo"
    assert trigger.config["_matched_from"] == "Ray"
