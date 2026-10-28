from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.api import schedules
from app.core.domain.agents import AgentStatus
from app.core.json_types import json_loads_object
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.db import session
from app.db.connection import DbConnection
from app.records.agent import AgentRecord
from app.records.schedule import AgentScheduleRecord
from app.records.schedule_manual_run import ScheduleManualRunRecord
from app.records.user import UserRecord
from test_schedule_dao import NOW, _Pool, _schedule, pool
from test_scheduler import clock
from test_scheduler_tasks import owner

__all__ = ["clock", "owner", "pool"]


@dataclass
class _HTTP:
    client: httpx.AsyncClient
    row: AgentScheduleRecord
    agent: AgentRecord
    get_schedule: AsyncMock
    update: AsyncMock
    activity: AsyncMock

    @property
    def url(self) -> str:
        return f"/agents/{self.row.agent_id}/schedules/{self.row.id}"


@pytest.fixture
async def api(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_HTTP]:
    row = _schedule()
    pool.rows[row.id] = row
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=user.id, status=AgentStatus.RUNNING)
    app = FastAPI()
    app.include_router(schedules.router, dependencies=[Depends(session.bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(schedules, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    get_schedule = AsyncMock(return_value=row)
    update = AsyncMock(return_value=row)
    monkeypatch.setattr(schedules.agent_schedule_dao, "get_for_agent", get_schedule)
    monkeypatch.setattr(schedules.agent_schedule_dao, "update", update)
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", AsyncMock(return_value=("", "")))
    monkeypatch.setattr("app.services.llm.call_agent_llm_with_tools", AsyncMock(return_value="reply"))
    activity = AsyncMock()
    monkeypatch.setattr("app.services.activity_logger.log_activity", activity)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield _HTTP(client, row, agent, get_schedule, update, activity)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("state", "expected"), [("stopped", 409), ("expired", 403), ("missing", 404)])
async def test_manual_rejects_before_launch_or_count_when_ineligible(
    api: _HTTP, pool: _Pool, state: str, expected: int
) -> None:
    # Given an expired-and-stopped agent, a stopped agent, or a missing schedule.
    api.agent.status = AgentStatus.STOPPED
    api.agent.is_expired = state == "expired"
    if state == "missing":
        api.get_schedule.return_value = None
    # When manually triggering through HTTP.
    response = await api.client.post(api.url + "/run")
    # Then the contract error is returned before bookkeeping or execution.
    assert response.status_code == expected
    assert pool.rows[api.row.id] == api.row
    api.update.assert_not_awaited()
    api.activity.assert_not_awaited()


async def test_manual_accepts_disabled_schedule_after_commit_without_advancing_next(
    api: _HTTP,
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a disabled schedule with its existing cron slot.
    row = replace(api.row, is_enabled=False)
    api.get_schedule.return_value = row
    pool.rows[row.id] = row
    run = ScheduleManualRunRecord(uuid4(), row.id, row.agent_id, row.created_by, row.instruction)
    accept = AsyncMock(return_value=run)
    monkeypatch.setattr(schedules.schedule_manual_run_dao, "accept", accept)
    # When a manual request is accepted without starting an execution callback.
    response = await api.client.post(api.url + "/run")
    # Then it returns its durable identity without changing cron or start bookkeeping.
    assert response.status_code == 200
    assert response.json() == {"status": "triggered", "schedule_id": str(row.id), "run_id": str(run.id)}
    assert pool.rows[row.id] == row
    api.update.assert_not_awaited()
    api.activity.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {"cron_expr": "invalid"},
        {"cron_expr": "invalid", "is_enabled": False},
        {"is_enabled": True},
    ],
)
async def test_update_rejects_invalid_effective_cron_before_write(api: _HTTP, payload: dict[str, object]) -> None:
    # Given an invalid supplied cron, or invalid stored cron being enabled.
    api.get_schedule.return_value = replace(api.row, cron_expr="invalid", is_enabled=False)
    # When updating through HTTP.
    response = await api.client.patch(api.url, json=payload)
    # Then bad cron never reaches a write, including while disabling.
    assert response.status_code == 400
    api.update.assert_not_awaited()


async def test_update_can_disable_stored_invalid_cron_without_replacing_it(api: _HTTP) -> None:
    # Given legacy invalid cron that needs disabling.
    api.get_schedule.return_value = replace(api.row, cron_expr="invalid")
    # When disabling without supplying cron.
    response = await api.client.patch(api.url, json={"is_enabled": False})
    # Then the slot clears without requiring repair of the stored cron.
    assert response.status_code == 200
    assert api.update.await_args_list[0].kwargs["obj_in"] == {"is_enabled": False, "next_run_at": None}


async def test_history_retains_deleted_schedule_logs_and_filters_other_schedules(
    api: _HTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given retained history plus another schedule's activity, without a live schedule.
    api.get_schedule.return_value = None
    log_id = uuid4()
    rows = [
        {
            "id": log_id,
            "agent_id": api.row.agent_id,
            "action_type": "schedule_run",
            "created_at": NOW,
            "summary": "summary",
            "detail_json": {"schedule_id": str(api.row.id), "instruction": "Report", "reply": "reply"},
        }
        for _ in range(20)
    ]
    fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr(DbConnection, "fetchall", fetch)
    # When reading history through the HTTP surface.
    response = await api.client.get(api.url + "/history")
    # Then the retained public payload is filtered and capped at twenty records.
    assert response.status_code == 200
    assert (
        response.json()
        == [
            {
                "id": str(log_id),
                "created_at": NOW.isoformat(),
                "summary": "summary",
                "instruction": "Report",
                "reply": "reply",
                "status": "unknown",
            }
        ]
        * 20
    )
    api.get_schedule.assert_not_awaited()
    query, params = fetch.await_args_list[0].args
    assert "action_type = 'schedule_run'" in query
    assert "detail_json ->> 'schedule_id' = %(schedule_id)s" in query
    assert "ORDER BY created_at DESC NULLS LAST, id DESC LIMIT %(limit)s" in query
    assert params == {"agent_id": api.row.agent_id, "schedule_id": str(api.row.id), "limit": 20}


@pytest.mark.parametrize("enabled", [False, True])
async def test_create_preserves_enabled_and_initial_accounting_contract(
    api: _HTTP,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    # Given a valid cron and the existing create DAO boundary.
    create = AsyncMock(return_value=api.row)
    monkeypatch.setattr(schedules.agent_schedule_dao, "create", create)
    # When creating an enabled or disabled schedule through HTTP.
    response = await api.client.post(
        api.url.rsplit("/", 1)[0] + "/",
        json={
            "name": "Daily",
            "instruction": "Report",
            "cron_expr": "0 12 * * *",
            "is_enabled": enabled,
        },
    )
    # Then initial accounting stays zero and only enabled schedules receive a slot.
    assert response.status_code == 201
    values = create.await_args_list[0].kwargs["obj_in"]
    assert values["run_count"] == 0
    assert values["is_enabled"] is enabled
    assert (values["next_run_at"] is not None) is enabled


@pytest.mark.parametrize("method", ["post", "patch", "delete"])
async def test_mutations_remain_creator_only(api: _HTTP, method: str) -> None:
    api.agent.creator_id = uuid4()
    path = api.url.rsplit("/", 1)[0] + "/" if method == "post" else api.url
    response = await api.client.request(method, path, json={"name": "Daily", "cron_expr": "0 12 * * *"})
    assert response.status_code == 403
    api.get_schedule.assert_not_awaited()
    api.update.assert_not_awaited()


@pytest.mark.parametrize(
    ("started", "finished", "outcome", "expected"),
    [
        (False, False, "cancelled", "pending"),
        (True, False, "success", "running"),
        (True, True, "success", "success"),
        (True, True, "failure", "failure"),
        (False, True, "cancelled", "cancelled"),
        (True, True, "legacy", "unknown"),
        (False, True, None, "unknown"),
    ],
)
async def test_manual_receipt_projects_timestamps_without_private_fields(
    api: _HTTP, monkeypatch: pytest.MonkeyPatch, started: bool, finished: bool, outcome: str | None, expected: str
) -> None:
    # Given a persisted run containing private snapshot, key and lease fields.
    run = ScheduleManualRunRecord(
        uuid4(),
        api.row.id,
        api.row.agent_id,
        api.agent.creator_id,
        "Private instruction",
        idempotency_key="Private key",
        created_at=NOW,
        started_at=NOW if started else None,
        finished_at=NOW if finished else None,
        outcome=outcome,
        lease_token=uuid4(),
        lease_expires_at=NOW,
    )
    get = AsyncMock(return_value=run)
    monkeypatch.setattr(schedules.schedule_manual_run_dao, "get_scoped", get, raising=False)
    monkeypatch.setattr(
        "app.services.schedule_manual_control.check_agent_access", AsyncMock(return_value=(api.agent, "manage"))
    )
    # When reading a scoped receipt over HTTP.
    response = await api.client.get(api.url + f"/runs/{run.id}")
    # Then only public identities, lifecycle timestamps and pending capability are exposed.
    assert response.status_code == 200
    body = json_loads_object(response.content)
    assert set(body) == {
        "run_id",
        "schedule_id",
        "agent_id",
        "requester_id",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "can_cancel",
    }
    assert [body[key] for key in ("run_id", "schedule_id", "agent_id", "requester_id")] == [
        str(run.id),
        str(run.schedule_id),
        str(run.agent_id),
        str(run.requester_id),
    ]
    assert body["status"] == expected
    assert body["can_cancel"] is (expected == "pending")
    created_at = body["created_at"]
    assert isinstance(created_at, str)
    assert datetime.fromisoformat(created_at) == NOW
    assert (body["started_at"] is not None) is started
    assert (body["finished_at"] is not None) is finished


@pytest.mark.parametrize("method", ["post", "patch"])
@pytest.mark.parametrize("cutoff", ["2026-10-16T12:00:00", "2026-10-16", 1792152000, 1792152000.5, "1792152000", True])
async def test_cutoff_rejects_non_offset_datetime_strings(
    api: _HTTP, monkeypatch: pytest.MonkeyPatch, method: str, cutoff: object
) -> None:
    # Given a non-instant or a value that datetime coercion could treat as a timestamp.
    path = api.url.rsplit("/", 1)[0] + "/" if method == "post" else api.url
    create = AsyncMock(return_value=api.row)
    monkeypatch.setattr(schedules.agent_schedule_dao, "create", create)
    # When creating or updating through the JSON boundary.
    response = await api.client.request(
        method, path, json={"name": "Daily", "cron_expr": "0 12 * * *", "ends_at": cutoff}
    )
    # Then validation rejects it before writing.
    assert response.status_code == 422
    create.assert_not_awaited()
    api.update.assert_not_awaited()


@pytest.mark.parametrize("payload", [{"name": "Renamed"}, {"timezone": "Asia/Ho_Chi_Minh"}])
async def test_patch_omission_preserves_cutoff_instant(api: _HTTP, payload: dict[str, object]) -> None:
    # Given a saved cutoff whose offset differs from the recurrence timezone.
    cutoff = datetime.fromisoformat("2026-10-18T17:00:00+05:30")
    api.get_schedule.return_value = replace(api.row, ends_at=cutoff)
    # When editing unrelated fields, including the recurrence timezone.
    response = await api.client.patch(api.url, json=payload)
    # Then omission never rewrites the instant.
    assert response.status_code == 200
    assert "ends_at" not in api.update.await_args_list[0].kwargs["obj_in"]


async def test_cutoff_only_patch_retains_late_eligible_slot(api: _HTTP) -> None:
    # Given an overdue occurrence that remains eligible even though the cutoff is past.
    due_at = NOW - timedelta(days=2)
    api.get_schedule.return_value = replace(api.row, next_run_at=due_at)
    # When adding a cutoff without changing recurrence.
    response = await api.client.patch(api.url, json={"ends_at": (NOW - timedelta(days=1)).isoformat()})
    # Then the due occurrence is not silently replaced by a calculation from now.
    assert response.status_code == 200
    assert api.update.await_args_list[0].kwargs["obj_in"]["next_run_at"] == due_at


@pytest.mark.parametrize("cutoff", [None, "2026-10-16T12:00:00Z", "2026-10-16T05:00:00-07:00"])
async def test_create_round_trips_nullable_offset_cutoff(
    api: _HTTP, monkeypatch: pytest.MonkeyPatch, cutoff: str | None
) -> None:
    # Given a valid future UTC instant represented with null, Z, or a negative offset.
    expected = datetime.fromisoformat(cutoff) if cutoff is not None else None
    create = AsyncMock(return_value=replace(api.row, ends_at=expected))
    monkeypatch.setattr(schedules.agent_schedule_dao, "create", create)
    # When creating through JSON.
    response = await api.client.post(
        api.url.rsplit("/", 1)[0] + "/", json={"name": "Daily", "cron_expr": "0 12 * * *", "ends_at": cutoff}
    )
    # Then the database receives a typed instant and the API serializes it without losing its offset.
    assert response.status_code == 201
    assert create.await_args_list[0].kwargs["obj_in"]["ends_at"] == expected
    assert schedules.ScheduleOut.model_validate(response.json()).ends_at == expected


@pytest.mark.parametrize(
    "payload", [{"cron_expr": "0 13 * * *"}, {"timezone": "Asia/Ho_Chi_Minh"}, {"is_enabled": True}]
)
async def test_recurrence_patch_caps_future_at_saved_cutoff(
    api: _HTTP, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    # Given a saved cutoff exactly at the newly computed recurrence.
    api.get_schedule.return_value = replace(api.row, ends_at=NOW)
    monkeypatch.setattr(schedules, "compute_next_run", lambda *_args, **_kwargs: NOW)
    # When changing recurrence without supplying a new cutoff.
    response = await api.client.patch(api.url, json=payload)
    # Then equality exhausts only the future slot, not the schedule or current ownership.
    assert response.status_code == 200
    values = api.update.await_args_list[0].kwargs["obj_in"]
    assert values["next_run_at"] is None
    assert values.get("is_enabled", True) is True
    assert "lease_token" not in values


async def test_clear_cutoff_keeps_disabled_schedule_unscheduled(api: _HTTP) -> None:
    # Given a disabled schedule with a finite cutoff and no next occurrence.
    api.get_schedule.return_value = replace(api.row, is_enabled=False, next_run_at=None, ends_at=NOW)
    # When removing the cutoff.
    response = await api.client.patch(api.url, json={"ends_at": None})
    # Then clearing does not implicitly enable it.
    assert response.status_code == 200
    assert api.update.await_args_list[0].kwargs["obj_in"] == {"ends_at": None, "next_run_at": None}
