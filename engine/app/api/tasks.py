"""Task management API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.dao.task_dao import task_dao, task_log_dao
from app.dao.user_dao import user_dao
from app.records.task import TaskRecord
from app.records.user import UserRecord
from app.schemas.schemas import TaskCreate, TaskLogCreate, TaskLogOut, TaskOut, TaskUpdate

router = APIRouter(prefix="/agents/{agent_id}/tasks", tags=["tasks"])


async def _enrich_task_out(task: TaskRecord) -> TaskOut:
    """Convert TaskRecord to TaskOut with creator_username populated."""
    out = TaskOut.model_validate(task)
    if task.created_by:
        user = await user_dao.get_with_identity(task.created_by)
        if user:
            out.creator_username = user.username
    return out


@router.get("/", response_model=list[TaskOut])
async def list_tasks(
    agent_id: uuid.UUID,
    status_filter: str | None = None,
    type_filter: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
) -> list[TaskOut]:
    """List tasks for an agent."""
    _ = await check_agent_access(current_user, agent_id)
    tasks_list = await task_dao.list_for_agent(agent_id, status=status_filter, type=type_filter)
    creator_ids = {t.created_by for t in tasks_list if t.created_by}
    creator_map = await user_dao.usernames_for_ids(list(creator_ids)) if creator_ids else {}
    out_list: list[TaskOut] = []
    for t in tasks_list:
        t_out = TaskOut.model_validate(t)
        t_out.creator_username = creator_map.get(t.created_by)
        out_list.append(t_out)
    return out_list


@router.post("/", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(agent_id: uuid.UUID, data: TaskCreate, current_user: UserRecord = Depends(get_current_user)):
    """Create a new task for an agent."""
    _ = await check_agent_access(current_user, agent_id)
    task = await task_dao.create(
        obj_in={
            "agent_id": agent_id,
            "title": data.title,
            "description": data.description,
            "type": data.type,
            "priority": data.priority,
            "due_date": data.due_date,
            "created_by": current_user.id,
            "supervision_target_name": data.supervision_target_name,
            "supervision_channel": data.supervision_channel,
            "remind_schedule": data.remind_schedule,
        }
    )

    task_out = await _enrich_task_out(task)

    # Fire background execution for todo tasks (row already committed via DAO)
    if data.type == "todo":
        from app.api.background_tasks import schedule_background_task
        from app.services.task_executor import execute_task

        _ = schedule_background_task(execute_task(task.id, agent_id), "execute task")

    return task_out


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    agent_id: uuid.UUID, task_id: uuid.UUID, data: TaskUpdate, current_user: UserRecord = Depends(get_current_user)
):
    """Update a task."""
    _ = await check_agent_access(current_user, agent_id)
    task = await task_dao.get_for_agent(task_id, agent_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    updated = await task_dao.update(db_obj=task, obj_in=data.model_dump(exclude_unset=True))
    return await _enrich_task_out(updated)


@router.get("/{task_id}/logs", response_model=list[TaskLogOut])
async def get_task_logs(agent_id: uuid.UUID, task_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Get progress logs for a task."""
    _ = await check_agent_access(current_user, agent_id)
    logs = await task_log_dao.list_for_task(task_id)
    return [TaskLogOut.model_validate(log) for log in logs]


@router.post("/{task_id}/logs", response_model=TaskLogOut, status_code=status.HTTP_201_CREATED)
async def add_task_log(
    agent_id: uuid.UUID, task_id: uuid.UUID, data: TaskLogCreate, current_user: UserRecord = Depends(get_current_user)
):
    """Add a progress log entry to a task."""
    _ = await check_agent_access(current_user, agent_id)
    # Ensure task belongs to agent
    task = await task_dao.get_for_agent(task_id, agent_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    log = await task_log_dao.create(obj_in={"task_id": task_id, "content": data.content})
    return TaskLogOut.model_validate(log)


@router.post("/{task_id}/trigger")
async def trigger_task(agent_id: uuid.UUID, task_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Manually trigger a supervision task execution (for testing)."""
    from app.core.permissions import is_agent_expired

    agent, _access = await check_agent_access(current_user, agent_id)
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")

    task = await task_dao.get_for_agent(task_id, agent_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.api.background_tasks import schedule_background_task
    from app.services.task_executor import execute_task

    _ = schedule_background_task(execute_task(task.id, agent_id), "execute task")

    return {"status": "triggered", "task_id": str(task_id)}
