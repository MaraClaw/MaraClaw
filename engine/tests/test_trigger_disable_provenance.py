from collections.abc import Mapping
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dao.base import BaseDAO
from app.dao.trigger_dao import agent_trigger_dao
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord
from app.services.trigger_runtime.executions import build_execution_runtime_trigger


@pytest.mark.parametrize("enabled", [True, False])
async def test_enable_write_takes_over_disable_provenance(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    # Given an existing disabled trigger and a captured base update.
    trigger = AgentTriggerRecord(uuid4(), uuid4(), is_enabled=False)
    update = AsyncMock(return_value=trigger)
    monkeypatch.setattr(BaseDAO, "update", update)
    changes = {"is_enabled": enabled}
    # When the caller writes the enable state, including False -> False.
    await agent_trigger_dao.update(db_obj=trigger, obj_in=changes)
    # Then provenance is cleared without mutating the caller's input.
    update.assert_awaited_once_with(db_obj=trigger, obj_in={"is_enabled": enabled, "self_disabled": False})
    assert changes == {"is_enabled": enabled}


@pytest.mark.parametrize("changes", [{"name": "renamed"}, {"is_enabled": False, "self_disabled": True}])
async def test_other_updates_pass_input_unchanged(
    monkeypatch: pytest.MonkeyPatch, changes: Mapping[str, object]
) -> None:
    # Given a write that does not implicitly take over provenance.
    trigger = AgentTriggerRecord(uuid4(), uuid4())
    update = AsyncMock(return_value=trigger)
    monkeypatch.setattr(BaseDAO, "update", update)
    # When updating through the trigger DAO.
    await agent_trigger_dao.update(db_obj=trigger, obj_in=changes)
    # Then the same mapping reaches BaseDAO unchanged.
    assert update.await_args is not None
    assert update.await_args.kwargs["obj_in"] is changes


@pytest.mark.parametrize("column", [None, False, True])
def test_record_reads_disable_provenance_with_legacy_default(column: bool | None) -> None:
    # Given a row with or without the provenance column.
    row: dict[str, object] = {"id": uuid4(), "agent_id": uuid4(), "focus_ref": None}
    if column is not None:
        row["self_disabled"] = column
    # When decoding the database row.
    trigger = AgentTriggerRecord.from_row(row)
    # Then the explicit value survives and old rows default to False.
    assert trigger.self_disabled is (column is True)


def test_runtime_trigger_preserves_disable_provenance() -> None:
    # Given a self-disabled trigger and its execution.
    trigger = AgentTriggerRecord(uuid4(), uuid4(), is_enabled=False, self_disabled=True)
    execution = TriggerExecutionRecord(uuid4(), trigger.id, trigger.agent_id)
    # When making the invocation-local copy.
    runtime = build_execution_runtime_trigger(trigger, execution)
    # Then provenance survives without becoming trigger configuration.
    assert runtime.self_disabled is True
    assert "self_disabled" not in runtime.config
