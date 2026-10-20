"""Schedule API - CRUD for agent cron jobs."""

import base64
import uuid
from datetime import datetime
from functools import partial
from typing import Annotated, ClassVar, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from app.core.json_types import json_as_bool, json_as_str_or, object_mapping_from
from app.core.permissions import check_agent_access, is_agent_creator, is_agent_expired
from app.core.security import get_current_user
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.user_dao import user_dao
from app.records.activity_log import AgentActivityLogRecord
from app.records.user import UserRecord
from app.services.schedule_time import ScheduleTimezoneError, validate_timezone
from app.services.scheduler import compute_next_run

router = APIRouter(prefix="/agents/{agent_id}/schedules", tags=["schedules"])
ScheduleTimezone = Annotated[str, AfterValidator(validate_timezone)]


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    instruction: str = Field(default="", max_length=5000)
    cron_expr: str = Field(min_length=1, max_length=100)
    timezone: ScheduleTimezone = "UTC"
    is_enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    instruction: str | None = None
    cron_expr: str | None = None
    timezone: ScheduleTimezone = "UTC"
    is_enabled: bool | None = None


class ScheduleOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    instruction: str
    cron_expr: str
    timezone: str
    is_enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int
    created_by: uuid.UUID | None = None
    creator_username: str | None = None
    created_at: datetime | None = None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)


class ScheduleHistoryItem(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: uuid.UUID
    created_at: str | None
    summary: str
    instruction: str
    reply: str
    status: Literal["success", "failure", "cancelled", "unknown"]

    @classmethod
    def from_record(cls, log: AgentActivityLogRecord) -> ScheduleHistoryItem:
        detail = object_mapping_from(log.detail_json)
        match detail.get("status"):
            case "success":
                run_status = "success"
            case "failure" | "failed":
                run_status = "failure"
            case "cancelled":
                run_status = "cancelled"
            case _:
                run_status = "unknown"
        return cls(
            id=log.id,
            created_at=log.created_at.isoformat() if log.created_at is not None else None,
            summary=log.summary,
            instruction=json_as_str_or(detail.get("instruction")),
            reply=json_as_str_or(detail.get("reply")),
            status=run_status,
        )


class ScheduleHistoryPage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    items: list[ScheduleHistoryItem]
    next_cursor: str | None


class _HistoryCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    agent_id: uuid.UUID
    schedule_id: uuid.UUID
    created_at: AwareDatetime | None
    id: uuid.UUID


def _validated_next_run(cron_expr: str, timezone: str) -> datetime:
    try:
        next_run = compute_next_run(cron_expr, timezone=timezone)
    except ScheduleTimezoneError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if next_run is None:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {cron_expr}")
    return next_run


@router.get("/", response_model=list[ScheduleOut])
async def list_schedules(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> list[ScheduleOut]:
    """List all schedules for an agent."""
    _ = await check_agent_access(current_user, agent_id)
    schedules = await agent_schedule_dao.list_for_agent(agent_id)
    creator_ids = {s.created_by for s in schedules if s.created_by}
    creator_map = await user_dao.usernames_for_ids(list(creator_ids)) if creator_ids else {}
    out_list: list[ScheduleOut] = []
    for s in schedules:
        s_out = ScheduleOut.model_validate(s)
        s_out.creator_username = creator_map.get(s.created_by)
        out_list.append(s_out)
    return out_list


@router.post("/", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    agent_id: uuid.UUID, data: ScheduleCreate, current_user: UserRecord = Depends(get_current_user)
):
    """Create a new schedule for an agent."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")

    next_run = _validated_next_run(data.cron_expr, data.timezone)

    sched = await agent_schedule_dao.create(
        obj_in={
            "agent_id": agent_id,
            "name": data.name,
            "instruction": data.instruction,
            "cron_expr": data.cron_expr,
            "timezone": data.timezone,
            "is_enabled": data.is_enabled,
            "next_run_at": next_run if data.is_enabled else None,
            "created_by": current_user.id,
            "run_count": 0,
        }
    )
    return ScheduleOut.model_validate(sched)


@router.patch("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    data: ScheduleUpdate,
    current_user: UserRecord = Depends(get_current_user),
):
    """Update a schedule."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")

    sched = await agent_schedule_dao.get_for_agent(schedule_id, agent_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")

    updates = object_mapping_from(data.model_dump(exclude_unset=True))
    if "cron_expr" in updates or "is_enabled" in updates or "timezone" in updates:
        is_enabled = (
            sched.is_enabled if "is_enabled" not in updates else json_as_bool(updates["is_enabled"], sched.is_enabled)
        )
        cron_expr = (
            sched.cron_expr if "cron_expr" not in updates else json_as_str_or(updates["cron_expr"], sched.cron_expr)
        )
        timezone = data.timezone if "timezone" in updates else sched.timezone
        next_run = (
            _validated_next_run(cron_expr, timezone)
            if is_enabled or "cron_expr" in updates or "timezone" in updates
            else None
        )
        updates["next_run_at"] = next_run if is_enabled else None

    updated = await agent_schedule_dao.update(db_obj=sched, obj_in=updates)
    return ScheduleOut.model_validate(updated)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    agent_id: uuid.UUID, schedule_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
):
    """Delete a schedule."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")

    sched = await agent_schedule_dao.get_for_agent(schedule_id, agent_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")

    _ = await agent_schedule_dao.delete(id=sched.id)


@router.post("/{schedule_id}/run")
async def trigger_schedule(
    agent_id: uuid.UUID, schedule_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
):
    """Manually trigger a schedule execution."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired and cannot be triggered.")

    sched = await agent_schedule_dao.get_for_agent(schedule_id, agent_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")

    if agent.status != "running":
        raise HTTPException(status_code=409, detail="Agent is not running")

    from app.services.scheduler import _execute_schedule
    from app.services.scheduler_tasks import schedule_execution

    schedule_execution(partial(_execute_schedule, sched.id, sched.agent_id, sched.instruction))

    return {"status": "triggered", "schedule_id": str(schedule_id)}


@router.get("/{schedule_id}/history", response_model=list[ScheduleHistoryItem])
async def get_schedule_history(
    agent_id: uuid.UUID, schedule_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> list[ScheduleHistoryItem]:
    """Get execution history for a schedule from activity logs."""
    _ = await check_agent_access(current_user, agent_id)

    rows = await agent_activity_log_dao.list_schedule_history(agent_id, schedule_id, limit=20)
    return [ScheduleHistoryItem.from_record(log) for log in rows]


@router.get("/{schedule_id}/history/page", response_model=ScheduleHistoryPage)
async def get_schedule_history_page(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    current_user: UserRecord = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> ScheduleHistoryPage:
    _ = await check_agent_access(current_user, agent_id)
    before = None
    if cursor is not None:
        try:
            payload = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            position = _HistoryCursor.model_validate_json(payload)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid history cursor") from error
        if position.agent_id != agent_id or position.schedule_id != schedule_id:
            raise HTTPException(status_code=400, detail="History cursor does not match this schedule")
        before = (position.created_at, position.id)
    rows = await agent_activity_log_dao.list_schedule_history(agent_id, schedule_id, limit=limit + 1, before=before)
    items = [ScheduleHistoryItem.from_record(log) for log in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        position = _HistoryCursor(
            version=1, agent_id=agent_id, schedule_id=schedule_id, created_at=last.created_at, id=last.id
        )
        next_cursor = base64.urlsafe_b64encode(position.model_dump_json().encode()).decode().rstrip("=")
    return ScheduleHistoryPage(items=items, next_cursor=next_cursor)
