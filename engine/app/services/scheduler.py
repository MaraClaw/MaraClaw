"""Lightweight asyncio scheduler for agent cron jobs.

Runs as a background task inside the FastAPI process.
Every 30 seconds, checks for schedules whose next_run_at <= now
and executes them by calling the LLM with the schedule's instruction.
"""

import asyncio
import uuid
from datetime import UTC, datetime

from croniter import croniter

from app.core.logging import logger

_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _observe_background_task(task: asyncio.Task[None]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("Scheduled task failed")


def compute_next_run(cron_expr: str, after: datetime | None = None) -> datetime | None:
    """Compute the next run time from a cron expression."""
    try:
        base = after or datetime.now(UTC)
        cron = croniter(cron_expr, base)
        return cron.get_next(datetime).replace(tzinfo=UTC)
    except Exception as e:
        logger.error(f"Invalid cron expression '{cron_expr}': {e}")
        return None


async def _execute_schedule(schedule_id: uuid.UUID, agent_id: uuid.UUID, instruction: str):
    """Execute a single schedule by calling the LLM with the instruction."""
    try:
        from app.core.permissions import is_agent_expired
        from app.dao.agent_dao import agent_dao

        agent = await agent_dao.get(agent_id)
        if not agent:
            logger.warning(f"Schedule {schedule_id}: agent {agent_id} not found")
            return

        if agent.status != "running":
            logger.info(f"Schedule {schedule_id}: agent {agent.name} not running, skipping")
            return

        if is_agent_expired(agent):
            logger.info(f"Schedule {schedule_id}: agent {agent.name} has expired, skipping")
            return

        from app.services.agent_context import build_agent_context
        from app.services.llm import call_agent_llm_with_tools

        static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent.name, agent.role_description or "")
        system_prompt = f"{static_prompt}\n\n{dynamic_prompt}"

        user_prompt = f"[Scheduled task] {instruction}"

        reply = await call_agent_llm_with_tools(
            db=None,
            agent_id=agent_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_rounds=50,
            session_id=str(schedule_id),
        )

        from app.services.activity_logger import log_activity

        await log_activity(
            agent_id,
            "schedule_run",
            f"Scheduled task executed: {instruction[:60]}",
            detail={"schedule_id": str(schedule_id), "instruction": instruction, "reply": reply[:500]},
        )

        logger.info(f"Schedule {schedule_id} executed for agent {agent.name}: {reply[:80]}")

    except Exception as e:
        logger.exception(f"Schedule {schedule_id} execution error: {e}")


async def _tick():
    """One scheduler tick: find and execute due schedules."""
    from app.dao.schedule_dao import agent_schedule_dao
    from app.services.audit_logger import write_audit_log

    now = datetime.now(UTC)

    try:
        due_schedules = await agent_schedule_dao.list_due(now)

        if due_schedules:
            await write_audit_log("schedule_tick", {"due_count": len(due_schedules)})

        for sched in due_schedules:
            next_run = compute_next_run(sched.cron_expr, now)
            _ = await agent_schedule_dao.update(
                db_obj=sched,
                obj_in={
                    "last_run_at": now,
                    "next_run_at": next_run,
                    "run_count": (sched.run_count or 0) + 1,
                },
            )

            await write_audit_log(
                "schedule_fire",
                {
                    "schedule_id": str(sched.id),
                    "name": sched.name,
                    "instruction": sched.instruction[:100],
                    "next_run": str(next_run),
                },
                agent_id=sched.agent_id,
            )

            task = asyncio.create_task(_execute_schedule(sched.id, sched.agent_id, sched.instruction))
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_observe_background_task)
            logger.info(f"Triggered schedule '{sched.name}' (next: {next_run})")

    except Exception as e:
        logger.exception(f"Scheduler tick error: {e}")
        await write_audit_log("schedule_error", {"error": str(e)[:300]})


async def start_scheduler():
    """Start the background scheduler loop. Call from FastAPI startup."""
    logger.info("🕐 Agent scheduler started (30s interval)")
    while True:
        await _tick()
        await asyncio.sleep(30)
