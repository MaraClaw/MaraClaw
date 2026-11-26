"""Materialize a database inventory page, then observe its guests without a database checkout."""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from fastapi import HTTPException

from app.dao.agent_lifecycle_projection_dao import CompanyRuntimePageQuery, agent_lifecycle_projection_dao
from app.dao.company_admission_dao import company_admission_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.schemas.admin_companies import (
    CompanyRuntimeAgentOut,
    CompanyRuntimeObservationOut,
    CompanyRuntimePageOut,
    CompanyRuntimeWorkspaceOut,
)
from app.schemas.agent_runtime_loss import runtime_loss_out
from app.services.admin_companies import MAX_PAGE_SIZE, decode_runtime_cursor, encode_runtime_cursor
from app.services.agent_manager import agent_manager
from app.services.agent_resource_pressure_docker import GuestInspection, inspect_guests
from app.services.agent_work_eligibility import AgentWorkPolicyContext, policy_blocking_codes
from app.services.docker_capacity import DockerBusyError, DockerPriority, run_docker

INVENTORY_INSPECT_TIMEOUT_SECONDS: Final = 5.0


async def company_runtime_page(company_id: UUID, cursor: str | None, limit: int) -> CompanyRuntimePageOut:
    after = decode_runtime_cursor(cursor, company_id) if cursor is not None else None
    page_size = min(max(limit, 1), MAX_PAGE_SIZE)
    async with connection_ctx() as db:
        tenant = await company_admission_dao.tenant(company_id)
        if tenant is None:
            raise HTTPException(404, "Company not found")
        rows = await agent_lifecycle_projection_dao.company_page(
            db, CompanyRuntimePageQuery(company_id, after, page_size)
        )
        page = rows[:page_size]
        projections = await agent_lifecycle_projection_dao.many(db, [agent.id for agent in page])
        owners = await user_dao.get_many_with_identity(list({agent.creator_id for agent in page}))
        as_of = datetime.now(UTC)
        items: list[CompanyRuntimeAgentOut] = []
        for agent in page:
            workspace = projections.workspaces.get(agent.id)
            items.append(
                CompanyRuntimeAgentOut(
                    agent_id=agent.id,
                    name=agent.name,
                    creator_id=agent.creator_id,
                    recorded_status=agent.status,
                    recorded_container_id=agent.container_id,
                    runtime_loss=runtime_loss_out(projections.losses.get(agent.id)),
                    start=projections.starts.get(agent.id),
                    upgrade_in_progress=agent.id in projections.upgrading,
                    workspace=CompanyRuntimeWorkspaceOut(
                        used_bytes=workspace.used_bytes,
                        limit_bytes=workspace.limit_bytes,
                        measured_at=workspace.measured_at,
                        over_since=workspace.over_since,
                    )
                    if workspace is not None
                    else None,
                    blocking_codes=policy_blocking_codes(
                        agent,
                        AgentWorkPolicyContext(
                            owners.get(agent.creator_id),
                            tenant,
                            agent.id in projections.transferring,
                            as_of,
                        ),
                    ),
                    observation=CompanyRuntimeObservationOut(state="missing"),
                )
            )
    references = list(
        dict.fromkeys(
            agent.container_id for agent in page if agent.container_id is not None and agent.container_id != ""
        )
    )
    docker = agent_manager.docker_client
    inspections: dict[str, GuestInspection] | None = {}
    if references:
        try:
            inspections = (
                await run_docker(
                    inspect_guests,
                    docker,
                    references,
                    INVENTORY_INSPECT_TIMEOUT_SECONDS,
                    priority=DockerPriority.BACKGROUND,
                )
                if docker is not None
                else None
            )
        except DockerBusyError:
            # An observation the busy lane cannot serve is unavailable, like one Docker cannot answer.
            inspections = None
    return CompanyRuntimePageOut(
        company_id=company_id,
        as_of=as_of,
        observation_available=inspections is not None,
        items=[item.model_copy(update={"observation": _observation(item, inspections)}) for item in items],
        next_cursor=encode_runtime_cursor(company_id, page[-1].id) if len(rows) > page_size else None,
    )


def _observation(
    agent: CompanyRuntimeAgentOut,
    inspections: dict[str, GuestInspection] | None,
) -> CompanyRuntimeObservationOut:
    if agent.recorded_container_id is None or agent.recorded_container_id == "":
        return CompanyRuntimeObservationOut(state="missing")
    if inspections is None:
        return CompanyRuntimeObservationOut(state="unavailable")
    guest = inspections.get(agent.recorded_container_id)
    if guest is None:
        return CompanyRuntimeObservationOut(state="missing")
    if guest.owner != str(agent.agent_id):
        return CompanyRuntimeObservationOut(state="foreign")
    return CompanyRuntimeObservationOut(
        state="observed",
        container_status=guest.status,
        running=guest.running,
        started_at=guest.started_at,
        finished_at=guest.finished_at,
        exit_code=guest.exit_code,
        restart_count_since_last_start=guest.restart_count,
        oom_killed_since_last_start=guest.oom_killed,
    )
