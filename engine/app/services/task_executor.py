"""Background task executor - runs LLM to complete tasks automatically.

Uses the same agent context (soul, memory, skills, relationships, tools)
as the chat dialog. Supports tool-calling loop for autonomous execution.
"""

import uuid
from datetime import UTC, datetime

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.task_dao import task_dao, task_log_dao


async def execute_task(task_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Execute a task using the agent's configured LLM with full context.

    Uses the same context as chat dialog: build_agent_context for system prompt,
    agent tools for tool-calling, and a multi-round tool loop.

    Flow:
      - todo tasks: pending → doing → done
      - supervision tasks: pending → doing → pending (stays active, just logs result)
    """
    logger.info(f"[TaskExec] Starting task {task_id} for agent {agent_id}")

    # Step 1: Mark as doing
    task = await task_dao.get(task_id)
    if not task:
        logger.warning(f"[TaskExec] Task {task_id} not found")
        return

    _ = await task_dao.update(db_obj=task, obj_in={"status": "doing"})
    _ = await task_log_dao.create(obj_in={"task_id": task_id, "content": "🤖 Starting task execution..."})

    task_title = task.title
    task_description = task.description or ""
    task_type = task.type  # 'todo' or 'supervision'
    supervision_target = task.supervision_target_name or ""

    # Step 2: Load agent
    agent = await agent_dao.get(agent_id)
    if not agent:
        await _log_error(task_id, "Digital employee not found")
        if task_type == "supervision":
            await _restore_supervision_status(task_id)
        return
    agent_name = agent.name

    # Step 3: Build full agent context (same as chat dialog)
    from app.services.agent_context import build_agent_context

    static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, agent.role_description or "")

    # Add task-execution-specific instructions
    task_addendum = """

## Task Execution Mode

You are now in TASK EXECUTION MODE (not a conversation). A task has been assigned to you.
- Focus on completing the task as thoroughly as possible.
- Break down complex tasks into steps and execute each step.
- Use your tools actively to gather information, send messages, read/write files, etc.
- Provide a detailed execution report at the end.
- If the task involves contacting someone, use `send_feishu_message` to reach them.
- If the task requires data or information, use your tools to fetch it.
- Do NOT ask the user follow-up questions - take initiative and complete the task autonomously.
"""
    dynamic_prompt += task_addendum
    system_prompt = f"{static_prompt}\n\n{dynamic_prompt}"

    # Build user prompt
    if task_type == "supervision":
        user_prompt = f"[Supervision Task] {task_title}"
        if task_description:
            user_prompt += f"\nTask description: {task_description}"
        if supervision_target:
            user_prompt += f"\nSupervision target: {supervision_target}"
        user_prompt += (
            "\n\nComplete this supervision task: contact the target, learn their progress, and report the result."
        )
    else:
        user_prompt = f"[Task Execution] {task_title}"
        if task_description:
            user_prompt += f"\nTask description: {task_description}"
        user_prompt += "\n\nComplete this task carefully and provide a detailed execution result."

    # Step 4: Call LLM with unified failover support
    from app.services.llm import call_agent_llm_with_tools

    try:
        logger.info(f"[TaskExec] Calling LLM with tools for task: {task_title}")

        reply = await call_agent_llm_with_tools(
            db=None,
            agent_id=agent_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_rounds=50,
            session_id=str(task_id),
        )

        logger.info(f"[TaskExec] LLM reply: {reply[:80]}")
    except Exception as e:
        error_msg = str(e) or repr(e)
        logger.error(f"[TaskExec] Error: {error_msg}")
        await _log_error(task_id, f"Execution error: {error_msg[:150]}")
        if task_type == "supervision":
            await _restore_supervision_status(task_id)
        return

    # Step 5: Save result and update status
    task = await task_dao.get(task_id)
    if task:
        if task_type == "supervision":
            # Supervision tasks stay active; just log the result
            _ = await task_dao.update(db_obj=task, obj_in={"status": "pending"})
            _ = await task_log_dao.create(
                obj_in={"task_id": task_id, "content": f"✅ Supervision completed\n\n{reply}"}
            )
        else:
            _ = await task_dao.update(
                db_obj=task,
                obj_in={"status": "done", "completed_at": datetime.now(UTC)},
            )
            _ = await task_log_dao.create(obj_in={"task_id": task_id, "content": f"✅ Task completed\n\n{reply}"})
        logger.info(f"[TaskExec] Task {task_id} {'logged' if task_type == 'supervision' else 'completed'}!")

    # Log activity
    from app.services.activity_logger import log_activity

    await log_activity(
        agent_id,
        "task_updated",
        f"{'Supervision' if task_type == 'supervision' else 'Task'} execution: {task_title[:60]}",
        detail={"task_id": str(task_id), "task_type": task_type, "title": task_title, "reply": reply[:500]},
        related_id=task_id,
    )


async def _log_error(task_id: uuid.UUID, message: str) -> None:
    """Add an error log to the task."""
    logger.error(f"[TaskExec] Error for {task_id}: {message}")
    _ = await task_log_dao.create(obj_in={"task_id": task_id, "content": f"❌ {message}"})


async def _restore_supervision_status(task_id: uuid.UUID) -> None:
    """Restore supervision task status back to pending after a failed execution."""
    task = await task_dao.get(task_id)
    if task and task.status == "doing":
        _ = await task_dao.update(db_obj=task, obj_in={"status": "pending"})
