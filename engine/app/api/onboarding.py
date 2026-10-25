"""Company onboarding APIs."""

import uuid
from datetime import UTC, datetime
from typing import Any, assert_never

from fastapi import APIRouter, Depends, HTTPException
from httpx import HTTPError
from pydantic import BaseModel, Field

from app.core.domain.agents import AgentPermissionScope, AgentStatus
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.llm_dao import llm_model_dao
from app.dao.onboarding_dao import user_tenant_onboarding_dao
from app.dao.template_dao import agent_template_dao
from app.dao.tenant_dao import tenant_dao
from app.db.session import flush_request_transaction
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.records.onboarding import UserTenantOnboardingRecord
from app.records.user import UserRecord
from app.schemas.organization_response_onboarding import (
    OnboardingStatusPayload,
    PersonalAssistantCreatedPayload,
    PersonalAssistantPayload,
    PersonalAssistantResponse,
    PersonalAssistantStart,
)
from app.schemas.schemas import AgentCreate
from app.services.agent_lifecycle import LifecycleFailure, LifecycleSuccess, start_agent_guest
from app.services.agent_manager import agent_manager
from app.services.agent_provisioning import AgentProvisioning, agent_provisioning, provision_agent
from app.services.enterprise_llm import assert_distinct_model_slots, model_usable_in_tenant
from app.services.openclaw_keys import write_gateway_api_key
from app.services.openclaw_update_exclusion import locked_fresh_agent
from app.services.openclaw_update_gate import OpenClawUpdateGateError

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingStartRequest(BaseModel):
    entry_mode: str = Field(default="create", pattern="^(create|join)$")


class PersonalAssistantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    personality: str = Field(default="warm", max_length=64)
    work_style: str = Field(default="concise", max_length=64)
    boundaries: str = Field(default="", max_length=1000)


def _status_payload(row: UserTenantOnboardingRecord | None) -> OnboardingStatusPayload:
    return {
        "exists": row is not None,
        "status": row.status if row else "not_started",
        "current_step": row.current_step if row else "company",
        "entry_mode": row.entry_mode if row else None,
        "personal_assistant_agent_id": str(row.personal_assistant_agent_id)
        if row and row.personal_assistant_agent_id
        else None,
        "completed_at": row.completed_at.isoformat() if row and row.completed_at else None,
    }


async def _get_row(user: UserRecord) -> UserTenantOnboardingRecord | None:
    if not user.tenant_id:
        return None
    return await user_tenant_onboarding_dao.get_for_user_tenant(user.id, user.tenant_id)


async def _ensure_row(user: UserRecord, entry_mode: str) -> UserTenantOnboardingRecord:
    if not user.tenant_id:
        raise HTTPException(status_code=400, detail="Company is required before onboarding")
    row = await _get_row(user)
    if row:
        if row.status == "completed":
            return row
        updates: dict[str, Any] = {"entry_mode": entry_mode}
        if row.current_step == "company":
            updates["current_step"] = "assistant"
        return await user_tenant_onboarding_dao.update(db_obj=row, obj_in=updates) or row

    await user_tenant_onboarding_dao.insert_ignore(
        user_id=user.id,
        tenant_id=user.tenant_id,
        entry_mode=entry_mode,
        current_step="assistant",
        status="in_progress",
    )

    row = await _get_row(user)
    if not row:
        raise HTTPException(status_code=500, detail="Failed to start onboarding")
    if row.status != "completed":
        updates = {"entry_mode": entry_mode}
        if row.current_step == "company":
            updates["current_step"] = "assistant"
        row = await user_tenant_onboarding_dao.update(db_obj=row, obj_in=updates) or row
    return row


def _usable_slot_id(
    tenant_id: uuid.UUID | None,
    model_id: uuid.UUID | None,
    loaded: dict[uuid.UUID, LLMModelRecord],
) -> uuid.UUID | None:
    if model_id is None:
        return None
    model = loaded.get(model_id)
    if model is None or not model_usable_in_tenant(model, tenant_id):
        return None
    return model_id


async def _tenant_model_ids(
    tenant_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]:
    if not tenant_id:
        return None, None, None
    tenant = await tenant_dao.get(tenant_id)
    if tenant and tenant.default_model_id:
        primary = tenant.default_model_id
        secondary = getattr(tenant, "default_secondary_model_id", None)
        fallback = getattr(tenant, "default_fallback_model_id", None)
    else:
        primary = await llm_model_dao.first_enabled_id_for_tenant(tenant_id)
        secondary = getattr(tenant, "default_secondary_model_id", None) if tenant else None
        fallback = getattr(tenant, "default_fallback_model_id", None) if tenant else None
    wanted = [mid for mid in (primary, secondary, fallback) if mid]
    loaded = {row.id: row for row in await llm_model_dao.get_many(wanted)} if wanted else {}
    primary = _usable_slot_id(tenant_id, primary, loaded)
    secondary = _usable_slot_id(tenant_id, secondary, loaded)
    fallback = _usable_slot_id(tenant_id, fallback, loaded)
    if secondary == primary:
        secondary = None
    if fallback in (primary, secondary):
        fallback = None
    assert_distinct_model_slots(primary, secondary, fallback)
    return primary, secondary, fallback


async def _create_personal_assistant(
    context: AgentProvisioning,
    data: PersonalAssistantRequest,
) -> tuple[AgentRecord, str]:
    template = await agent_template_dao.get_by_name("Private Assistant")
    primary_model_id, secondary_model_id, fallback_model_id = await _tenant_model_ids(context.tenant_id)
    personality_note = f"Personality: {data.personality}. Work style: {data.work_style}."
    boundaries = data.boundaries.strip()
    bio = (
        "A private assistant for daily coordination, notes, follow-ups, drafts, and light planning. "
        f"{personality_note}" + (f" Boundaries: {boundaries}" if boundaries else "")
    )

    return await provision_agent(
        context,
        AgentCreate(
            name=data.name.strip(),
            role_description="Private Assistant",
            bio=bio,
            permission_scope_type=AgentPermissionScope.USER,
            primary_model_id=primary_model_id,
            secondary_model_id=secondary_model_id,
            fallback_model_id=fallback_model_id,
            template_id=template.id if template else None,
            autonomy_policy=template.default_autonomy_policy if template else None,
        ),
    )


async def _record_start_failure(agent_id: uuid.UUID) -> None:
    try:
        async with locked_fresh_agent(agent_id) as fresh:
            if fresh.status in ("idle", "stopped"):
                _ = await agent_dao.update(db_obj=fresh, obj_in={"status": AgentStatus.ERROR})
                await flush_request_transaction()
    except OpenClawUpdateGateError, HTTPException:
        return


async def _start_personal_assistant(
    agent: AgentRecord, data: PersonalAssistantRequest, raw_key: str | None
) -> PersonalAssistantStart:
    from app.api.relationships import _regenerate_relationships_file

    try:
        if raw_key is not None:
            write_gateway_api_key(agent, raw_key)
        await agent_manager.initialize_agent_files(
            agent,
            personality=f"Personality: {data.personality}. Work style: {data.work_style}.",
            boundaries=data.boundaries.strip(),
        )
        await _regenerate_relationships_file(agent.id)
        outcome = await start_agent_guest(agent.id)
    except OpenClawUpdateGateError as exc:
        outcome = LifecycleFailure(exc.status_code, exc.code, exc.rejection()["content"])
    except HTTPException, OSError, ValueError, HTTPError:
        outcome = LifecycleFailure(502, "guest_start_failed", "Guest preparation failed")
    match outcome:
        case LifecycleSuccess():
            return {"status": "started"}
        case LifecycleFailure(code=code, message=message):
            await _record_start_failure(agent.id)
            return {"status": "failed", "code": code, "message": message}
        case unreachable:
            assert_never(unreachable)


@router.get("/status", response_model=None)
async def get_onboarding_status(current_user: UserRecord = Depends(get_current_user)) -> OnboardingStatusPayload:
    """Return onboarding state for the current user/company."""
    return _status_payload(await _get_row(current_user))


@router.post("/start", response_model=None)
async def start_onboarding(
    data: OnboardingStartRequest, current_user: UserRecord = Depends(get_current_user)
) -> OnboardingStatusPayload:
    """Start or resume onboarding for the current user/company."""
    row = await _ensure_row(current_user, data.entry_mode)
    return _status_payload(row)


@router.post("/personal-assistant", response_model=None)
async def create_personal_assistant(
    data: PersonalAssistantRequest, current_user: UserRecord = Depends(get_current_user)
) -> PersonalAssistantResponse:
    """Create the user's private assistant and advance onboarding."""
    if current_user.tenant_id is None:
        raise HTTPException(400, "Company is required before creating a personal assistant")
    raw_key = None
    async with agent_provisioning(current_user, current_user.tenant_id) as context:
        row = await _ensure_row(context.actor, "join")
        agent = await agent_dao.get(row.personal_assistant_agent_id) if row.personal_assistant_agent_id else None
        updates: dict[str, object] = {"current_step": "opening"}
        if agent is None:
            agent, raw_key = await _create_personal_assistant(context, data)
            updates.update(personal_assistant_agent_id=agent.id, status="in_progress")
        row = await user_tenant_onboarding_dao.update(db_obj=row, obj_in=updates)
    await flush_request_transaction()
    start = await _start_personal_assistant(agent, data, raw_key)
    payload: PersonalAssistantPayload | PersonalAssistantCreatedPayload = {"id": str(agent.id), "name": agent.name}
    if raw_key is not None:
        payload = {"id": str(agent.id), "name": agent.name, "api_key": raw_key}
    return {
        "agent": payload,
        "onboarding": _status_payload(row),
        "start": start,
    }


@router.post("/complete", response_model=None)
async def complete_onboarding(current_user: UserRecord = Depends(get_current_user)) -> OnboardingStatusPayload:
    """Mark the current user/company onboarding as completed."""
    row = await _get_row(current_user)
    if not row:
        row = await _ensure_row(current_user, "join")
    row = (
        await user_tenant_onboarding_dao.update(
            db_obj=row,
            obj_in={
                "status": "completed",
                "current_step": "completed",
                "completed_at": datetime.now(UTC),
            },
        )
        or row
    )
    return _status_payload(row)
