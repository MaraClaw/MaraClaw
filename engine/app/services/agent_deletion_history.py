import json
from datetime import UTC, datetime
from uuid import UUID

from app.dao.task_dao import task_dao, task_log_dao


def _serialize_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def snapshot_task_history(agent_id: UUID) -> str | None:
    tasks = await task_dao.list_for_agent(agent_id, ascending=True)
    if not tasks:
        return None
    task_items: list[dict[str, object]] = []
    for task in tasks:
        logs = await task_log_dao.list_for_task(task.id)
        task_items.append(
            {
                "id": str(task.id),
                "title": task.title,
                "description": task.description,
                "type": task.type,
                "status": task.status,
                "priority": task.priority,
                "assignee": task.assignee,
                "created_by": str(task.created_by),
                "due_date": _serialize_dt(task.due_date),
                "supervision_target_user_id": (
                    str(task.supervision_target_user_id) if task.supervision_target_user_id else None
                ),
                "supervision_target_name": task.supervision_target_name,
                "supervision_channel": task.supervision_channel,
                "remind_schedule": task.remind_schedule,
                "created_at": _serialize_dt(task.created_at),
                "updated_at": _serialize_dt(task.updated_at),
                "completed_at": _serialize_dt(task.completed_at),
                "logs": [
                    {"id": str(log.id), "content": log.content, "created_at": _serialize_dt(log.created_at)}
                    for log in logs
                ],
            }
        )
    return json.dumps(
        {"agent_id": str(agent_id), "archived_at": datetime.now(UTC).isoformat(), "tasks": task_items},
        ensure_ascii=False,
        indent=2,
    )
