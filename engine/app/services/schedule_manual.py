from datetime import UTC, datetime
from functools import partial

from fastapi import HTTPException

from app.core.json_types import JsonObject
from app.core.logging import logger, new_trace_id
from app.core.permissions import check_agent_access, is_agent_expired
from app.core.security import load_user_principal
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.schedule_manual_run_dao import schedule_manual_run_dao
from app.db.session import connection_ctx
from app.records.schedule_manual_run import ScheduleManualRunRecord
from app.services.agent_context import build_agent_context
from app.services.llm import call_agent_llm_with_tools
from app.services.llm.background_guard import ScheduleLeaseLostError
from app.services.schedule_lease import in_fresh_schedule_context
from app.services.schedule_manual_lease import ManualScheduleLeaseHold
from app.services.scheduler_tasks import schedule_execution


async def execute_manual_run(run: ScheduleManualRunRecord) -> None:
    new_trace_id()
    token = run.lease_token
    if token is None:
        raise ValueError("Manual execution requires a claimed run")
    detail: JsonObject = {
        "schedule_id": str(run.schedule_id),
        "run_id": str(run.id),
        "instruction": run.instruction,
        "status": "cancelled",
    }
    outcome = "cancelled"
    summary = f"Manual scheduled task cancelled: {run.instruction[:60]}"
    try:
        async with ManualScheduleLeaseHold(run.id, token) as hold:
            try:
                user = await load_user_principal(run.requester_id, fresh=True)
                agent, _access = await check_agent_access(user, run.agent_id, fresh=True)
            except HTTPException as exc:
                if exc.status_code not in (401, 403, 404):
                    raise
                agent = None
                detail["reason"] = "requester_ineligible"
            await hold.confirm()
            if agent is not None and agent.status == "running" and not is_agent_expired(agent):
                if not await in_fresh_schedule_context(schedule_manual_run_dao.start(run.id, token, datetime.now(UTC))):
                    raise ScheduleLeaseLostError("Manual run ownership was revoked before start")
                outcome = "failure"
                summary = f"Manual scheduled task failed: {run.instruction[:60]}"
                try:
                    static, dynamic = await build_agent_context(run.agent_id, agent.name, agent.role_description or "")
                    await hold.confirm()
                    result = await call_agent_llm_with_tools(
                        agent_id=run.agent_id,
                        system_prompt=f"{static}\n\n{dynamic}",
                        user_prompt=f"[Scheduled task] {run.instruction}",
                        max_rounds=50,
                        session_id=str(run.id),
                        guard=hold,
                    )
                    await hold.confirm()
                    outcome = result.status
                    detail["reply"] = result.content[:500]
                    if outcome == "success":
                        summary = f"Manual scheduled task executed: {run.instruction[:60]}"
                except ScheduleLeaseLostError:
                    raise
                except Exception as exc:
                    await hold.confirm()
                    detail["error"] = str(exc)[:500]
                    logger.bind(run_id=str(run.id)).exception("Manual schedule execution failed")
            detail["status"] = outcome
            await hold.confirm()

        async def finish() -> None:
            hold.ensure_held()
            async with connection_ctx():
                if not await schedule_manual_run_dao.finish(run, datetime.now(UTC), outcome):
                    raise ScheduleLeaseLostError("Manual run ownership was revoked before finish")
                _ = await agent_activity_log_dao.create(
                    obj_in={
                        "agent_id": run.agent_id,
                        "action_type": "schedule_run",
                        "summary": summary,
                        "detail_json": detail,
                        "related_id": None,
                    }
                )
                hold.ensure_held()

        await in_fresh_schedule_context(finish())
    except ScheduleLeaseLostError as exc:
        logger.bind(run_id=str(run.id)).info("Manual schedule stopped after lease loss: {}", exc)
    except Exception:
        logger.bind(run_id=str(run.id)).exception("Manual schedule recovery failed")


async def recover_manual_runs() -> None:
    new_trace_id()
    try:
        async with connection_ctx():
            runs = await schedule_manual_run_dao.claim_pending(datetime.now(UTC))
            for run in runs:
                schedule_execution(partial(execute_manual_run, run))
    except Exception:
        logger.exception("Manual schedule claim failed")
