import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.records.trigger import AgentTriggerRecord
from app.services.trigger_runtime.evaluator import evaluate_trigger


@pytest.mark.asyncio
async def test_evaluate_trigger_returns_false_for_non_numeric_interval() -> None:
    # Given an interval trigger with an invalid persisted interval value.
    now = datetime.now(UTC)
    trigger = AgentTriggerRecord(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="invalid-interval",
        reason="test",
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=None,
        cooldown_seconds=0,
        config={"minutes": "ten"},
        type="interval",
        created_at=now - timedelta(hours=1),
    )

    # When the trigger is evaluated.
    result = await evaluate_trigger(trigger, now)

    # Then it is not due rather than raising from the persisted value.
    assert result is False
