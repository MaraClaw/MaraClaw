from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException

from app.core.permissions import check_agent_access, is_agent_creator
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.schedule_manual_run_dao import schedule_manual_run_dao
from app.db.session import connection_ctx
from app.records.schedule_manual_run import ScheduleManualRunRecord
from app.records.user import UserRecord


async def authorized_manual_run(
    current_user: UserRecord, agent_id: UUID, schedule_id: UUID, run_id: UUID
) -> ScheduleManualRunRecord:
    agent, _access = await check_agent_access(current_user, agent_id, fresh=True)
    run = await schedule_manual_run_dao.get_scoped(run_id, schedule_id, agent_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Manual run not found")
    if run.requester_id != current_user.id and not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only requester or agent creator can manage this run")
    return run


async def cancel_manual_run(run: ScheduleManualRunRecord) -> ScheduleManualRunRecord:
    async with connection_ctx():
        cancelled = await schedule_manual_run_dao.cancel_pending(run, datetime.now(UTC))
        if cancelled is not None:
            _ = await agent_activity_log_dao.create(
                obj_in={
                    "agent_id": cancelled.agent_id,
                    "action_type": "schedule_run",
                    "summary": f"Manual scheduled task cancelled: {cancelled.instruction[:60]}",
                    "detail_json": {
                        "schedule_id": str(cancelled.schedule_id),
                        "run_id": str(cancelled.id),
                        "instruction": cancelled.instruction,
                        "status": "cancelled",
                        "reason": "user_cancelled",
                    },
                    "related_id": None,
                }
            )
            return cancelled
        current = await schedule_manual_run_dao.get_scoped(run.id, run.schedule_id, run.agent_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Manual run not found")
        if current.finished_at is not None and current.outcome == "cancelled":
            return current
        raise HTTPException(status_code=409, detail="Manual run has already started or finished")
