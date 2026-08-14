import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import logger
from app.dao.task_dao import task_dao
from app.services.agent_tool_exec.registry import ToolArguments, tool_arg_str


async def _sync_tasks_to_file(agent_id: uuid.UUID, ws: Path):
    """Sync tasks from DB to legacy tasks.json, if the file already exists."""
    tasks_path = ws / "tasks.json"
    if not tasks_path.exists():
        return
    try:
        tasks = await task_dao.list_for_agent(agent_id)

        task_list = [
            {
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "description": t.description or "",
                "created_at": t.created_at.isoformat() if t.created_at else "",
                "completed_at": t.completed_at.isoformat() if t.completed_at else "",
            }
            for t in tasks
        ]

        _ = tasks_path.write_text(
            json.dumps(task_list, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"[AgentTools] Failed to sync tasks: {e}")


async def _manage_tasks(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    ws: Path,
    args: ToolArguments,
) -> str:
    """Create / update / delete tasks in DB and sync to workspace."""
    action = args["action"]
    title = tool_arg_str(args["title"])
    if title is None:
        return "Missing required argument 'title'"

    if action == "create":
        task_type = args.get("task_type", "todo")
        task = await task_dao.create(
            obj_in={
                "agent_id": agent_id,
                "title": title,
                "description": args.get("description"),
                "type": task_type,
                "priority": args.get("priority", "medium"),
                "created_by": user_id,
                "status": "pending",
                "supervision_target_name": args.get("supervision_target_name"),
                "supervision_channel": args.get("supervision_channel", "feishu"),
                "remind_schedule": args.get("remind_schedule"),
            }
        )

        if task_type == "todo":
            from app.services.task_executor import execute_task

            execution_task = asyncio.create_task(execute_task(task.id, agent_id))
            del execution_task
            await _sync_tasks_to_file(agent_id, ws)
            return f"✅ Task created: {title} - auto-execution started"
        # Supervision task - reminder engine will pick it up
        target = args.get("supervision_target_name", "someone")
        schedule = args.get("remind_schedule", "not set")
        await _sync_tasks_to_file(agent_id, ws)
        return f"✅ Supervision task created: '{title}' - will remind {target} on schedule ({schedule})"

    if action == "update_status":
        task = await task_dao.find_first_by_title_ilike(agent_id, title)
        if not task:
            return f"No task found matching '{title}'"
        old = task.status
        updates: dict[str, object] = {"status": args["status"]}
        if args["status"] == "done":
            updates["completed_at"] = datetime.now(UTC)
        _ = await task_dao.update(db_obj=task, obj_in=updates)
        await _sync_tasks_to_file(agent_id, ws)
        return f"✅ Updated '{task.title}' from {old} to {args['status']}"

    if action == "delete":
        task = await task_dao.find_first_by_title_ilike(agent_id, title)
        if not task:
            return f"No task found matching '{title}'"
        task_title = task.title
        _ = await task_dao.delete_with_logs(task.id)
        await _sync_tasks_to_file(agent_id, ws)
        return f"✅ Task deleted: {task_title}"

    return f"Unknown action: {action}"
