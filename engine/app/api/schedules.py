"""Schedule API - CRUD for agent cron jobs."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.permissions import check_agent_access, is_agent_creator, is_agent_expired
from app.core.security import get_current_user
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.user import UserRecord
from app.services.scheduler import compute_next_run

router = APIRouter(prefix="/agents/{agent_id}/schedules", tags=["schedules"])


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    instruction: str = Field(default="", max_length=5000)
    cron_expr: str = Field(min_length=1, max_length=100)
    is_enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    instruction: str | None = None
    cron_expr: str | None = None
    is_enabled: bool | None = None


class ScheduleOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    instruction: str
    cron_expr: str
    is_enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int
    created_by: uuid.UUID | None = None
    creator_username: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[ScheduleOut])
async def list_schedules(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """List all schedules for an agent."""
    await check_agent_access(current_user, agent_id)
    schedules = await agent_schedule_dao.list_for_agent(agent_id)
    creator_ids = {s.created_by for s in schedules if s.created_by}
    creator_map = await user_dao.usernames_for_ids(list(creator_ids)) if creator_ids else {}
    out_list = []
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

    next_run = compute_next_run(data.cron_expr)
    if not next_run:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {data.cron_expr}")

    sched = await agent_schedule_dao.create(
        obj_in={
            "agent_id": agent_id,
            "name": data.name,
            "instruction": data.instruction,
            "cron_expr": data.cron_expr,
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

    updates = data.model_dump(exclude_unset=True)
    if "cron_expr" in updates or "is_enabled" in updates:
        is_enabled = updates.get("is_enabled", sched.is_enabled)
        cron_expr = updates.get("cron_expr", sched.cron_expr)
        updates["next_run_at"] = compute_next_run(cron_expr) if is_enabled else None

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

    await agent_schedule_dao.delete(id=sched.id)


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

    from app.api.background_tasks import schedule_background_task
    from app.services.scheduler import _execute_schedule

    schedule_background_task(
        _execute_schedule(sched.id, sched.agent_id, sched.instruction),
        "execute schedule",
    )

    await agent_schedule_dao.update(
        db_obj=sched,
        obj_in={
            "last_run_at": datetime.now(UTC),
            "run_count": (sched.run_count or 0) + 1,
        },
    )

    return {"status": "triggered", "schedule_id": str(schedule_id)}


@router.get("/{schedule_id}/history")
async def get_schedule_history(
    agent_id: uuid.UUID, schedule_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
):
    """Get execution history for a schedule from activity logs."""
    await check_agent_access(current_user, agent_id)

    async with connection_ctx() as conn:
        rows = await conn.fetchall(
            "SELECT id, created_at, summary, detail_json FROM agent_activity_logs "
            "WHERE agent_id = %(agent_id)s AND action_type = 'schedule_run' "
            "ORDER BY created_at DESC LIMIT 200",
            {"agent_id": agent_id},
        )

    history = []
    schedule_id_str = str(schedule_id)
    for log in rows:
        detail = log.get("detail_json") or {}
        if not isinstance(detail, dict):
            detail = dict(detail) if detail else {}
        if detail.get("schedule_id") != schedule_id_str:
            continue
        history.append(
            {
                "id": str(log["id"]),
                "created_at": log["created_at"].isoformat() if log.get("created_at") else None,
                "summary": log.get("summary"),
                "instruction": detail.get("instruction", ""),
                "reply": detail.get("reply", ""),
            }
        )
        if len(history) >= 20:
            break
    return history
