from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException

from app.core.notification_kinds import NotificationKind
from app.dao.agent_dao import agent_dao
from app.dao.notification_dao import notification_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.db import session
from app.records.agent import AgentRecord
from app.records.identity import IdentityRecord
from app.records.notification import NotificationRecord
from app.records.schedule import AgentScheduleRecord
from app.records.user import UserRecord
from app.services import scheduler
from app.services.llm.background_guard import BackgroundCallOutcome
from test_schedule_dao import _Pool, pool
from test_scheduler_periodic_lease_runtime import activities, occurrence, owner

__all__ = ["activities", "occurrence", "owner", "pool"]


@pytest.fixture
def notifications(occurrence: AgentScheduleRecord, pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    tenant = uuid4()
    principal = UserRecord(id=occurrence.created_by, tenant_id=tenant, identity=IdentityRecord(id=uuid4()))
    agent = AgentRecord(id=occurrence.agent_id, name="Private agent name", creator_id=uuid4(), tenant_id=tenant)
    monkeypatch.setattr("app.core.security.load_user_principal", AsyncMock(return_value=principal))
    monkeypatch.setattr("app.core.permissions.check_agent_access", AsyncMock(return_value=(agent, "use")))

    async def insert(*, obj_in):
        assert session.get_connection() is not None
        assert pool.rows[occurrence.id].pending_due_at is None
        return NotificationRecord.from_row({"id": uuid4(), **obj_in})

    create = AsyncMock(side_effect=insert)
    monkeypatch.setattr(notification_dao, "create", create)
    return create


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("failure", ["provider", "exception", "context"])
async def test_failure_alert_uses_snapshot_and_safe_content_when_live_preference_changes(
    occurrence, pool, activities, notifications, monkeypatch, snapshot, failure
):
    claim = replace(occurrence, pending_instruction="PRIVATE_PROMPT", pending_notify_on_failure=snapshot)
    pool.rows[claim.id] = replace(claim, notify_on_failure=not snapshot)
    target = (
        "app.services.agent_context.build_agent_context"
        if failure == "context"
        else "app.services.llm.call_agent_llm_with_tools"
    )
    result = AsyncMock(return_value=BackgroundCallOutcome("failure", "PRIVATE_REPLY"))
    if failure != "provider":
        result.side_effect = RuntimeError("PRIVATE_EXCEPTION")
    monkeypatch.setattr(target, result)

    await scheduler._execute_periodic_schedule(claim)

    assert len(activities) == 1
    assert notifications.await_count == int(snapshot)
    if snapshot:
        values = notifications.await_args_list[0].kwargs["obj_in"]
        assert values["type"] == NotificationKind.SYSTEM_ALERT.value
        assert values["user_id"] == claim.created_by
        assert values["agent_id"] is None
        assert values["ref_id"] == claim.id
        assert values["link"] == f"/app/agents/{claim.agent_id}/schedules"
        assert not any(
            secret in str(values)
            for secret in ("PRIVATE_PROMPT", "PRIVATE_REPLY", "PRIVATE_EXCEPTION", "Private agent name")
        )


async def test_repeated_completion_alerts_once_when_occurrence_is_already_finished(
    occurrence, pool, activities, notifications, monkeypatch
):
    claim = replace(occurrence, pending_notify_on_failure=True)
    pool.rows[claim.id] = claim
    monkeypatch.setattr(
        "app.services.llm.call_agent_llm_with_tools",
        AsyncMock(return_value=BackgroundCallOutcome("failure", "private")),
    )

    await scheduler._execute_periodic_schedule(claim)
    await scheduler._execute_periodic_schedule(claim)

    assert len(activities) == 1
    notifications.assert_awaited_once()


@pytest.mark.parametrize("exclusion", ["success", "skipped", "cancelled", "lost_lease", "stale_finish", "manual"])
async def test_alert_is_absent_when_execution_is_not_an_owned_periodic_failure(
    occurrence, pool, notifications, monkeypatch, activities, exclusion
):
    claim = replace(occurrence, pending_notify_on_failure=True)
    pool.rows[claim.id] = claim
    match exclusion:
        case "success":
            pass
        case "skipped":
            monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=None))
        case "cancelled":
            monkeypatch.setattr(
                "app.services.llm.call_agent_llm_with_tools", AsyncMock(side_effect=anyio.get_cancelled_exc_class())
            )
        case "lost_lease":
            pool.rows[claim.id] = replace(claim, lease_token=uuid4())
        case "stale_finish":
            monkeypatch.setattr(agent_schedule_dao, "finish_occurrence", AsyncMock(return_value=False))
            monkeypatch.setattr(
                "app.services.llm.call_agent_llm_with_tools",
                AsyncMock(return_value=BackgroundCallOutcome("failure", "private")),
            )
        case "manual":
            monkeypatch.setattr("app.services.activity_logger.log_activity", AsyncMock())
            monkeypatch.setattr(
                "app.services.llm.call_agent_llm_with_tools", AsyncMock(side_effect=RuntimeError("manual failure"))
            )
        case _:
            raise AssertionError(exclusion)

    if exclusion == "cancelled":
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await scheduler._execute_periodic_schedule(claim)
    elif exclusion == "manual":
        await scheduler._execute_schedule(claim.id, claim.agent_id, claim.instruction)
    else:
        await scheduler._execute_periodic_schedule(claim)

    notifications.assert_not_awaited()


@pytest.mark.parametrize("stage", ["principal", "permission"])
async def test_authorization_denial_finishes_without_alert_when_original_creator_is_ineligible(
    occurrence, pool, activities, notifications, monkeypatch, stage
):
    claim = replace(occurrence, pending_notify_on_failure=True)
    pool.rows[claim.id] = claim
    target = (
        "app.core.security.load_user_principal" if stage == "principal" else "app.core.permissions.check_agent_access"
    )
    rejected = AsyncMock(side_effect=HTTPException(403, "denied"))
    monkeypatch.setattr(target, rejected)
    monkeypatch.setattr(
        "app.services.llm.call_agent_llm_with_tools",
        AsyncMock(return_value=BackgroundCallOutcome("failure", "private")),
    )

    await scheduler._execute_periodic_schedule(claim)

    assert pool.rows[claim.id].pending_due_at is None
    assert len(activities) == 1
    assert rejected.await_args_list[0].kwargs == {"fresh": True}
    notifications.assert_not_awaited()
