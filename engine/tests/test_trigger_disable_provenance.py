from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.dao.base import BaseDAO
from app.dao.trigger_dao import agent_trigger_dao, trigger_execution_dao
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord
from app.services.trigger_runtime.executions import build_execution_runtime_trigger


@asynccontextmanager
async def no_session() -> AsyncGenerator[None]:
    yield


@pytest.mark.parametrize("enabled", [True, False])
async def test_enable_write_takes_over_disable_provenance(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    # Given an existing disabled trigger and a captured base update.
    trigger = AgentTriggerRecord(uuid4(), uuid4(), is_enabled=False)
    update = AsyncMock(return_value=trigger)
    monkeypatch.setattr(BaseDAO, "update", update)
    monkeypatch.setattr(agent_trigger_dao, "session", no_session)
    monkeypatch.setattr(trigger_execution_dao, "fail_pending_for_triggers", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_trigger_dao, "reschedule", AsyncMock(return_value={}))
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
    monkeypatch.setattr(agent_trigger_dao, "session", no_session)
    monkeypatch.setattr(agent_trigger_dao, "reschedule", AsyncMock(return_value={}))
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


async def test_disable_write_settles_pending_executions_in_the_same_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an enabled trigger and a recorded sequence of the base write and the settlement.
    trigger = AgentTriggerRecord(uuid4(), uuid4())
    steps: list[str] = []

    async def write(*_args: object, **_kwargs: object) -> AgentTriggerRecord:
        steps.append("write")
        return trigger

    async def settle(trigger_ids: Sequence[UUID]) -> list[UUID]:
        steps.append("settle")
        assert list(trigger_ids) == [trigger.id]
        return []

    async def reschedule(trigger_ids: Sequence[UUID]) -> dict[UUID, None]:
        steps.append("reschedule")
        return {}

    @asynccontextmanager
    async def session() -> AsyncGenerator[None]:
        steps.append("open")
        yield
        steps.append("close")

    monkeypatch.setattr(agent_trigger_dao, "reschedule", reschedule)
    monkeypatch.setattr(BaseDAO, "update", write)
    monkeypatch.setattr(agent_trigger_dao, "session", session)
    monkeypatch.setattr(trigger_execution_dao, "fail_pending_for_triggers", settle)
    # When a user, the limiter or a bulk caller disables it through the DAO.
    await agent_trigger_dao.update(db_obj=trigger, obj_in={"is_enabled": False})
    # Then the pending executions are settled and the schedule cleared between the write and the commit.
    assert steps == ["open", "write", "settle", "reschedule", "close"]


@pytest.mark.parametrize("changes", [{"is_enabled": True}, {"is_enabled": False, "self_disabled": True}, {"name": "x"}])
async def test_other_writes_settle_nothing(monkeypatch: pytest.MonkeyPatch, changes: Mapping[str, object]) -> None:
    # Given a write that is not a disable.
    trigger = AgentTriggerRecord(uuid4(), uuid4())
    monkeypatch.setattr(BaseDAO, "update", AsyncMock(return_value=trigger))
    monkeypatch.setattr(agent_trigger_dao, "session", no_session)
    monkeypatch.setattr(agent_trigger_dao, "reschedule", AsyncMock(return_value={}))
    settle = AsyncMock()
    monkeypatch.setattr(trigger_execution_dao, "fail_pending_for_triggers", settle)
    # When it runs.
    await agent_trigger_dao.update(db_obj=trigger, obj_in=changes)
    # Then no pending execution is settled: enabling, a self-disable and other columns keep their work.
    settle.assert_not_awaited()


class _Rows:
    def __init__(self, ids: list[UUID]) -> None:
        self.ids = ids

    async def fetchall(self, _sql: str, params: Mapping[str, object]) -> list[dict[str, object]]:
        self.params = params
        return [{"id": value} for value in self.ids]


@pytest.mark.parametrize(
    "disable",
    [
        lambda: agent_trigger_dao.disable_for_tenant(uuid4()),
        lambda: agent_trigger_dao.disable_for_creator(uuid4(), tenant_id=uuid4()),
        lambda: agent_trigger_dao.disable_for_agent(uuid4()),
    ],
)
async def test_bulk_disables_settle_the_pending_executions_of_every_disabled_trigger(
    monkeypatch: pytest.MonkeyPatch, disable: Callable[[], Awaitable[int]]
) -> None:
    # Given a bulk UPDATE that disables two triggers.
    disabled = [uuid4(), uuid4()]
    rows = _Rows(disabled)

    @asynccontextmanager
    async def session() -> AsyncGenerator[_Rows]:
        yield rows

    settle = AsyncMock(return_value=[])
    monkeypatch.setattr(agent_trigger_dao, "session", session)
    monkeypatch.setattr(trigger_execution_dao, "fail_pending_for_triggers", settle)
    # When a tenant, a creator or an agent is disabled.
    count = await disable()
    # Then exactly the disabled triggers have their pending executions settled.
    assert count == 2
    settle.assert_awaited_once_with(disabled)
