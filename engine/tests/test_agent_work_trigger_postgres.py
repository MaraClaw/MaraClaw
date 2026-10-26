from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db

from app.dao.chat_dao import chat_message_dao
from app.db.session import transaction
from app.records.trigger import AgentTriggerRecord
from app.services import llm
from app.services.trigger_runtime import ExecutionLeaseHold, invoker

__all__ = ["channel_db", "receipt_db"]


@pytest.mark.parametrize("phase", ["entry", "model", "delivery"])
async def test_trigger_rechecks_owner_before_each_new_work_boundary(channel_db: ChannelDatabase, monkeypatch, phase):
    fixture = channel_db
    model_id = uuid4()
    async with transaction(fixture.db.first):
        await fixture.db.first.execute(
            "INSERT INTO llm_models(id,tenant_id,provider,model,api_key_encrypted,label) "
            "VALUES (%(id)s,%(tenant)s,'anthropic','qa','test-key','QA')",
            {"id": model_id, "tenant": fixture.tenant_id},
        )
        await fixture.db.first.execute("UPDATE agents SET primary_model_id=%(id)s", {"id": model_id})
    trigger = AgentTriggerRecord(id=uuid4(), agent_id=fixture.db.agent.id, name="once", self_disabled=True)

    async def disable() -> None:
        async with transaction(fixture.db.second):
            await fixture.db.second.execute(
                "UPDATE users SET is_active=FALSE WHERE id=%(id)s", {"id": fixture.db.user.id}
            )

    insert = chat_message_dao.insert_message

    async def before_model(**kwargs):
        message = await insert(**kwargs)
        await disable()
        return message

    completed_side_effect: list[str] = []

    async def running_model(**_kwargs):
        await disable()
        completed_side_effect.append("completed")
        return "model reply"

    model = AsyncMock(side_effect=running_model if phase == "delivery" else None, return_value="model reply")
    monkeypatch.setattr(llm, "call_llm", model)
    if phase == "entry":
        await disable()
    if phase == "model":
        monkeypatch.setattr(chat_message_dao, "insert_message", before_model)
    async with transaction(fixture.db.first):
        async with ExecutionLeaseHold(()) as hold:
            error = await invoker._run_trigger_invocation(fixture.db.agent.id, [trigger], hold)
        roles = await fixture.db.first.fetchall("SELECT role FROM chat_messages ORDER BY created_at")
    assert error is not None
    assert "owner_inactive" in error
    assert [row["role"] for row in roles] == ([] if phase == "entry" else ["user"])
    assert model.await_count == (1 if phase == "delivery" else 0)
    assert completed_side_effect == (["completed"] if phase == "delivery" else [])
