"""Background task executor - runs LLM to complete tasks automatically.

Uses the same agent context (soul, memory, skills, relationships, tools)
as the chat dialog. Supports tool-calling loop for autonomous execution.
"""

import asyncio
import uuid
from dataclasses import dataclass

from app.core.domain.activity import ActivityAction
from app.core.domain.tasks import TaskStatus, TaskType
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.task_dao import task_dao
from app.records.task import TaskClaim
from app.services.agent_setup_lock import drain_on_cancel
from app.services.task_lease import TaskLeaseLostError, claim_task, run_under_lease


class _RunFailureError(Exception):
    """A run that cannot proceed; ``reason`` is what the task log records."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason: str = reason


@dataclass(frozen=True, slots=True)
class _Outcome:
    title: str
    task_type: TaskType
    reply: str


async def execute_task(task_id: uuid.UUID, agent_id: uuid.UUID, claim: TaskClaim | None = None) -> None:
    """Execute a task using the agent's configured LLM with full context.

    Uses the same context as chat dialog: build_agent_context for system prompt,
    agent tools for tool-calling, and a multi-round tool loop.

    Runs only under a held claim: the trigger route claims before it schedules and passes
    the claim in; every other caller claims here on entry. Whoever loses the claim does nothing.

    Flow:
      - todo tasks: pending → doing → done
      - supervision tasks: pending → doing → pending (stays active, just logs result)
      - any failure, or cancellation: back to pending with the reason in the task log
    """
    if claim is None:
        claim = await claim_task(task_id, agent_id)
        if claim is None:
            logger.info(f"[TaskExec] Task {task_id} is missing or already running; not starting another run")
            return
    logger.info(f"[TaskExec] Starting task {task_id} for agent {agent_id}")

    try:
        outcome = await run_under_lease(claim, lambda: _run(claim, agent_id))
        await _complete(claim, agent_id, outcome)
    except TaskLeaseLostError:
        logger.warning(f"[TaskExec] Task {task_id} lost its lease; its run was cancelled")
        await _release(claim, "Execution lease renewal failed; the run was cancelled")
        return
    except asyncio.CancelledError:
        await drain_on_cancel(_release(claim, "Execution was cancelled before it finished"))
        raise
    except _RunFailureError as failure:
        await _release(claim, failure.reason)
        return
    except Exception as error:
        message = str(error) or repr(error)
        logger.error(f"[TaskExec] Error: {message}")
        await _release(claim, f"Execution error: {message[:150]}")
        return


async def _run(claim: TaskClaim, agent_id: uuid.UUID) -> _Outcome:
    task_id = claim.task_id
    task = await task_dao.get_for_agent(task_id, agent_id)
    if not task:
        raise _RunFailureError("Task not found")
    task_title = task.title
    task_description = task.description or ""
    task_type = task.type  # 'todo' or 'supervision'
    supervision_target = task.supervision_target_name or ""

    # Load agent
    agent = await agent_dao.get(agent_id)
    if not agent:
        raise _RunFailureError("Digital employee not found")
    agent_name = agent.name

    # Build full agent context (same as chat dialog)
    from app.services.agent_context import build_agent_context

    try:
        static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, agent.role_description or "")
    except Exception as error:
        message = str(error) or repr(error)
        raise _RunFailureError(f"Failed to build agent context: {message[:150]}") from error

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
    if task_type == TaskType.SUPERVISION:
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

    # Call LLM with unified failover support
    from app.services.llm import call_agent_llm_with_tools

    logger.info(f"[TaskExec] Calling LLM with tools for task: {task_title}")

    reply = await call_agent_llm_with_tools(
        agent_id=agent_id,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_rounds=50,
        session_id=str(task_id),
    )

    logger.info(f"[TaskExec] LLM reply: {reply[:80]}")
    return _Outcome(title=task_title, task_type=task_type, reply=reply)


async def _complete(claim: TaskClaim, agent_id: uuid.UUID, outcome: _Outcome) -> None:
    """Record the result under the claim token; a run that lost its claim writes nothing."""
    task_id = claim.task_id
    supervision = outcome.task_type == TaskType.SUPERVISION
    if supervision:
        # Supervision tasks stay active; just log the result
        settled = await task_dao.complete(
            claim, status=TaskStatus.PENDING, log=f"✅ Supervision completed\n\n{outcome.reply}"
        )
    else:
        settled = await task_dao.complete(claim, status=TaskStatus.DONE, log=f"✅ Task completed\n\n{outcome.reply}")
    if not settled:
        logger.warning(f"[TaskExec] Task {task_id} no longer belongs to this run; its result was discarded")
        return
    logger.info(f"[TaskExec] Task {task_id} {'logged' if supervision else 'completed'}!")

    # Log activity
    from app.services.activity_logger import log_activity

    await log_activity(
        agent_id,
        ActivityAction.TASK_UPDATED,
        f"{'Supervision' if supervision else 'Task'} execution: {outcome.title[:60]}",
        detail={
            "task_id": str(task_id),
            "task_type": outcome.task_type.value,
            "title": outcome.title,
            "reply": outcome.reply[:500],
        },
        related_id=task_id,
    )


async def _release(claim: TaskClaim, reason: str) -> None:
    """Give the task back as pending with the reason logged; a stale claim changes nothing."""
    logger.error(f"[TaskExec] Error for {claim.task_id}: {reason}")
    try:
        released = await task_dao.release(claim, log=f"❌ {reason}")
    except Exception as error:
        logger.error(f"[TaskExec] Could not release task {claim.task_id}; lease recovery will: {error}")
        return
    if not released:
        logger.warning(f"[TaskExec] Task {claim.task_id} no longer belongs to this run; nothing was released")
