from typing import assert_never
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException

from app.core.domain.agents import AgentSharingMode, AgentStatus
from app.dao.agent_dao import agent_dao
from app.dao.company_transfer_dao import company_transfer_dao as transfers
from app.dao.company_transfer_disposition_sql import FREE_INTEGRATION_DELETES, NEUTRALIZE_NOTIFICATIONS_SQL
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.company_admission import CompanyAdmissionRecord
from app.records.company_transfer import CompanyTransferRecord, TransferChoice
from app.records.user import UserRecord
from app.services.company_transfer_guards import require_admission_transfer, require_default_source


async def apply_disposition(request: CompanyAdmissionRecord, user: UserRecord, fenced_ids: frozenset[UUID]) -> None:
    await require_default_source(user)
    transfer = await transfers.for_admission(request.id)
    await require_admission_transfer(user, request.target_tenant_id, transfer.id if transfer else None)
    agents = await agent_dao.list_for_creator(user.id, tenant_id=user.tenant_id)
    if frozenset(agent.id for agent in agents) != fenced_ids:
        raise HTTPException(409, {"code": "transfer_assets_changed", "retryable": True})
    if transfer is None:
        return
    async with connection_ctx() as db:
        for agent in sorted(agents, key=lambda item: item.id):
            match transfer.choice:
                case TransferChoice.PAID:
                    await migrate_agent(agent, transfer)
                case TransferChoice.FREE:
                    await seal_agent(agent, transfer)
                case unreachable:
                    assert_never(unreachable)
            await db.execute(
                "INSERT INTO company_transfer_runtime_cleanup(agent_id,transfer_request_id,choice) "
                "VALUES(%(agent)s,%(request)s,%(choice)s) "
                "ON CONFLICT(agent_id) DO UPDATE SET transfer_request_id=EXCLUDED.transfer_request_id, "
                "choice=EXCLUDED.choice,state='pending',attempts=0,last_error=NULL,completed_at=NULL",
                {"agent": agent.id, "request": transfer.id, "choice": transfer.choice.value},
            )
    if agents:
        from app.api.background_tasks import schedule_background_task
        from app.services.company_transfer_cleanup import reconcile_transfer_cleanup

        schedule_background_task(reconcile_transfer_cleanup(), description="complete transfer runtime cleanup")


async def migrate_agent(agent: AgentRecord, transfer: CompanyTransferRecord) -> None:
    async with connection_ctx() as db:
        params = {"agent": agent.id, "target": transfer.target_tenant_id}
        await db.execute(
            "DELETE FROM agent_tools a USING tools t WHERE a.tool_id=t.id AND a.agent_id=%(agent)s "
            "AND t.tenant_id IS NOT NULL AND t.tenant_id<>%(target)s",
            params,
        )
        await db.execute("DELETE FROM agent_permissions WHERE agent_id=%(agent)s", params)
        await db.execute("DELETE FROM agent_relationships WHERE agent_id=%(agent)s", params)
        await db.execute(
            "DELETE FROM agent_agent_relationships WHERE agent_id=%(agent)s OR target_agent_id=%(agent)s",
            params,
        )
        await db.execute("UPDATE published_pages SET tenant_id=%(target)s WHERE agent_id=%(agent)s", params)
        # Old automation leases cannot deliver into the destination with source-org context.
        await db.execute(
            "UPDATE agent_schedules SET is_enabled=false,pending_due_at=NULL,pending_instruction=NULL, "
            "lease_token=NULL,lease_expires_at=NULL,pending_started_at=NULL WHERE agent_id=%(agent)s",
            params,
        )
        await db.execute("UPDATE agent_triggers SET is_enabled=false WHERE agent_id=%(agent)s", params)
        models = await db.fetchone(
            "SELECT primary_model_id,secondary_model_id,fallback_model_id FROM agents WHERE id=%(agent)s",
            params,
        )
        bindings: dict[str, object] = {}
        if models is not None:
            for field, model_id in models.items():
                if model_id is not None and not await db.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM llm_models WHERE id=%(id)s AND tenant_id=%(tenant)s)",
                    {"id": model_id, "tenant": transfer.target_tenant_id},
                ):
                    bindings[field] = None
        _ = await agent_dao.update(
            db_obj=agent,
            obj_in={
                **bindings,
                "tenant_id": transfer.target_tenant_id,
                "status": AgentStatus.STOPPED,
                "api_key_hash": None,
                "access_mode": AgentSharingMode.PRIVATE,
                "heartbeat_enabled": False,
            },
        )


async def seal_agent(agent: AgentRecord, transfer: CompanyTransferRecord) -> None:
    holder_id = uuid5(NAMESPACE_URL, f"maraclaw:transfer-retention:{transfer.source_tenant_id}")
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO users(id,identity_id,tenant_id,display_name,role,is_active,is_genesis) "
            "VALUES(%(id)s,NULL,NULL,'Retained data','member',false,false) ON CONFLICT(id) DO NOTHING",
            {"id": holder_id},
        )
        await db.execute(
            "INSERT INTO company_transfer_retained_agents "
            "(agent_id,transfer_request_id,original_user_id,source_tenant_id,holder_user_id) "
            "VALUES(%(agent)s,%(request)s,%(user)s,%(source)s,%(holder)s)",
            {
                "agent": agent.id,
                "request": transfer.id,
                "user": transfer.applicant_user_id,
                "source": transfer.source_tenant_id,
                "holder": holder_id,
            },
        )
        params: dict[str, object] = {"agent": agent.id, "agents": [agent.id]}
        await db.execute(NEUTRALIZE_NOTIFICATIONS_SQL, params)
        await db.execute(
            "UPDATE tasks SET remind_schedule=NULL,supervision_target_user_id=NULL,supervision_channel=NULL "
            "WHERE agent_id=%(agent)s",
            params,
        )
        await db.execute(
            "DELETE FROM agent_agent_relationships WHERE agent_id=%(agent)s OR target_agent_id=%(agent)s",
            params,
        )
        for statement in FREE_INTEGRATION_DELETES:
            await db.execute(statement, params)
        _ = await agent_dao.update(
            db_obj=agent,
            obj_in={
                "creator_id": holder_id,
                "status": AgentStatus.STOPPED,
                "is_expired": True,
                "heartbeat_enabled": False,
                "api_key_hash": None,
                "primary_model_id": None,
                "secondary_model_id": None,
                "fallback_model_id": None,
                "autonomy_policy": {},
                "access_mode": AgentSharingMode.PRIVATE,
                "company_access_level": "",
                "gogcli_enabled": False,
                "setup_pending": False,
            },
        )
