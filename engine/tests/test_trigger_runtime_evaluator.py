import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.records.trigger import AgentTriggerRecord
from app.services.trigger_runtime.evaluator import evaluate_trigger


async def test_mark_fired_records_only_a_self_disable_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import session as session_module
    from app.services.trigger_runtime.evaluator import mark_trigger_fired
    from test_tenant_disable_dao import _FakePool, _FakeRawConnection

    # Given a recording connection, without a live database.
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    token = session_module._conn_ctx.set(None)
    trigger_id, now = uuid.uuid4(), datetime.now(UTC)
    # When recording a firing.
    try:
        await mark_trigger_fired(trigger_id, now)
    finally:
        session_module._conn_ctx.reset(token)
    # Then only an enabled -> disabled transition records self provenance.
    query, params = raw.executed[0]
    assert (
        "self_disabled = CASE WHEN is_enabled IS TRUE AND (type = 'once' OR "
        "(max_fires IS NOT NULL AND fire_count + 1 >= max_fires)) THEN TRUE ELSE self_disabled END"
    ) in " ".join(query.split())
    assert params == {"id": trigger_id, "now": now}


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
