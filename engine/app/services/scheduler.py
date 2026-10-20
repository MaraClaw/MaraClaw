"""Lightweight asyncio scheduler for agent cron jobs.

Runs as a background task inside the FastAPI process.
Every 30 seconds, checks for schedules whose next_run_at <= now
and executes them by calling the LLM with the schedule's instruction.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from functools import partial

from app.core.json_types import JsonObject
from app.core.logging import logger, new_trace_id
from app.db.session import connection_ctx
from app.records.schedule import AgentScheduleRecord
from app.services.schedule_time import next_local_run
from app.services.scheduler_tasks import schedule_execution


def compute_next_run(cron_expr: str, after: datetime | None = None, timezone: str = "UTC") -> datetime | None:
    """Return the first eligible local occurrence strictly after the base, in UTC."""
    return next_local_run(cron_expr, after or datetime.now(UTC), timezone)


async def _execute_schedule(schedule_id: uuid.UUID, agent_id: uuid.UUID, instruction: str) -> None:
    """Execute a single schedule by calling the LLM with the instruction."""
    new_trace_id()
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

        from app.dao.schedule_dao import agent_schedule_dao
        from app.services.activity_logger import log_activity

        await agent_schedule_dao.record_started(schedule_id, datetime.now(UTC))

        summary = f"Scheduled task failed: {instruction[:60]}"
        detail: JsonObject = {"schedule_id": str(schedule_id), "instruction": instruction, "status": "failure"}
        try:
            from app.services.agent_context import build_agent_context
            from app.services.llm import call_agent_llm_with_tools

            static_prompt, dynamic_prompt = await build_agent_context(
                agent_id, agent.name, agent.role_description or ""
            )
            system_prompt = f"{static_prompt}\n\n{dynamic_prompt}"

            user_prompt = f"[Scheduled task] {instruction}"

            reply = await call_agent_llm_with_tools(
                agent_id=agent_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_rounds=50,
                session_id=str(schedule_id),
            )
            summary = f"Scheduled task executed: {instruction[:60]}"
            detail["reply"] = reply[:500]
            detail["status"] = "success"
        except asyncio.CancelledError:
            summary = f"Scheduled task cancelled: {instruction[:60]}"
            detail["status"] = "cancelled"
            raise
        finally:
            await log_activity(agent_id, "schedule_run", summary, detail=detail)

        logger.info(f"Schedule {schedule_id} executed for agent {agent.name}: {reply[:80]}")

    except Exception as e:
        logger.exception(f"Schedule {schedule_id} execution error: {e}")


async def _execute_periodic_schedule(schedule: AgentScheduleRecord) -> None:
    """Run the claimed occurrence snapshot, leaving interrupted work recoverable."""
    new_trace_id()
    from app.core.permissions import is_agent_expired
    from app.dao.activity_log_dao import agent_activity_log_dao
    from app.dao.agent_dao import agent_dao
    from app.dao.schedule_dao import agent_schedule_dao
    from app.services.agent_context import build_agent_context
    from app.services.llm import call_agent_llm_with_tools
    from app.services.llm.background_guard import ScheduleLeaseLostError
    from app.services.schedule_lease import ScheduleLeaseHold, in_fresh_schedule_context

    token, instruction, due_at = schedule.lease_token, schedule.pending_instruction, schedule.pending_due_at
    if token is None or instruction is None or due_at is None:
        raise ValueError("Periodic execution requires a claimed schedule occurrence")
    detail: JsonObject = {
        "schedule_id": str(schedule.id),
        "instruction": instruction,
        "due_at": due_at.isoformat(),
        "status": "skipped",
    }
    summary = f"Scheduled task skipped: {instruction[:60]}"
    try:
        async with ScheduleLeaseHold(schedule.id, token) as hold:
            agent = await agent_dao.get(schedule.agent_id)
            await hold.confirm()
            if agent is not None and agent.status == "running" and not is_agent_expired(agent):
                started = await in_fresh_schedule_context(
                    agent_schedule_dao.start_occurrence(schedule.id, token, datetime.now(UTC))
                )
                if not started:
                    raise ScheduleLeaseLostError("Schedule occurrence ownership was revoked before start")
                await hold.confirm()
                summary = f"Scheduled task failed: {instruction[:60]}"
                detail["status"] = "failure"
                try:
                    static_prompt, dynamic_prompt = await build_agent_context(
                        schedule.agent_id, agent.name, agent.role_description or ""
                    )
                    await hold.confirm()
                    outcome = await call_agent_llm_with_tools(
                        agent_id=schedule.agent_id,
                        system_prompt=f"{static_prompt}\n\n{dynamic_prompt}",
                        user_prompt=f"[Scheduled task] {instruction}",
                        max_rounds=50,
                        session_id=str(schedule.id),
                        guard=hold,
                    )
                    await hold.confirm()
                    detail["status"] = outcome.status
                    detail["reply"] = outcome.content[:500]
                    if outcome.status == "success":
                        summary = f"Scheduled task executed: {instruction[:60]}"
                except ScheduleLeaseLostError:
                    raise
                except Exception as exc:
                    await hold.confirm()
                    detail["error"] = str(exc)[:500]
                    logger.bind(schedule_id=str(schedule.id)).exception("Periodic schedule execution failed")
            await hold.confirm()

        async def finish() -> None:
            hold.ensure_held()
            async with connection_ctx():
                if not await agent_schedule_dao.finish_occurrence(schedule.id, token, datetime.now(UTC)):
                    raise ScheduleLeaseLostError("Schedule occurrence ownership was revoked before finish")
                hold.ensure_held()
                _ = await agent_activity_log_dao.create(
                    obj_in={
                        "agent_id": schedule.agent_id,
                        "action_type": "schedule_run",
                        "summary": summary,
                        "detail_json": detail,
                        "related_id": None,
                    }
                )
                hold.ensure_held()

        await in_fresh_schedule_context(finish())
    except ScheduleLeaseLostError as exc:
        logger.bind(schedule_id=str(schedule.id)).info("Periodic schedule stopped after lease loss: {}", exc)
    except Exception:
        logger.bind(schedule_id=str(schedule.id)).exception("Periodic schedule execution error")


async def _tick() -> None:
    """One scheduler tick: find and execute due schedules."""
    new_trace_id()
    from app.dao.schedule_dao import agent_schedule_dao
    from app.services.audit_logger import write_audit_log

    now = datetime.now(UTC)

    try:
        async with connection_ctx():
            due_schedules = await agent_schedule_dao.claim_due(now, compute_next_run)
            for sched in due_schedules:
                schedule_execution(partial(_execute_periodic_schedule, sched))

        # Audit writes swallow errors, so they must not share the claim transaction.
        if due_schedules:
            await write_audit_log("schedule_tick", {"due_count": len(due_schedules)})

        for sched in due_schedules:
            await write_audit_log(
                "schedule_fire",
                {
                    "schedule_id": str(sched.id),
                    "name": sched.name,
                    "instruction": (sched.pending_instruction or "")[:100],
                    "next_run": str(sched.next_run_at),
                },
                agent_id=sched.agent_id,
            )

            logger.info(f"Triggered schedule '{sched.name}' (next: {sched.next_run_at})")

    except Exception as e:
        logger.exception(f"Scheduler tick error: {e}")
        await write_audit_log("schedule_error", {"error": str(e)[:300]})


async def start_scheduler() -> None:
    """Start the background scheduler loop. Call from FastAPI startup."""
    new_trace_id()
    logger.info("🕐 Agent scheduler started (30s interval)")
    while True:
        await _tick()
        await asyncio.sleep(30)
