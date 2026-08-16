"""Company onboarding APIs."""

import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao, agent_permission_dao
from app.dao.llm_dao import llm_model_dao
from app.dao.onboarding_dao import user_tenant_onboarding_dao
from app.dao.participant_dao import participant_dao
from app.dao.template_dao import agent_template_dao
from app.dao.tenant_dao import tenant_dao
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.records.onboarding import UserTenantOnboardingRecord
from app.records.user import UserRecord
from app.services.access_relationships import ensure_access_granted_platform_relationships
from app.services.enterprise_llm import assert_distinct_model_slots, model_usable_in_tenant

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingStatusPayload(TypedDict):
    exists: bool
    status: str
    current_step: str
    entry_mode: str | None
    personal_assistant_agent_id: str | None
    completed_at: str | None


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
    db: object | None,
    user: UserRecord,
    data: PersonalAssistantRequest,
) -> tuple[AgentRecord, str]:
    if not user.tenant_id:
        raise HTTPException(status_code=400, detail="Company is required before creating a personal assistant")

    template = await agent_template_dao.get_by_name("Private Assistant")
    primary_model_id, secondary_model_id, fallback_model_id = await _tenant_model_ids(user.tenant_id)
    personality_note = f"Personality: {data.personality}. Work style: {data.work_style}."
    boundaries = data.boundaries.strip()
    bio = (
        "A private assistant for daily coordination, notes, follow-ups, drafts, and light planning. "
        + f"{personality_note}"
        + (f" Boundaries: {boundaries}" if boundaries else "")
    )

    from app.services.openclaw_keys import mint_openclaw_gateway_key, write_gateway_api_key

    raw_key, key_hash = mint_openclaw_gateway_key()
    obj_in: dict[str, Any] = {
        "name": data.name.strip(),
        "role_description": "Private Assistant",
        "bio": bio,
        "creator_id": user.id,
        "tenant_id": user.tenant_id,
        "agent_type": "openclaw",
        "api_key_hash": key_hash,
        "primary_model_id": primary_model_id,
        "secondary_model_id": secondary_model_id,
        "fallback_model_id": fallback_model_id,
        "template_id": template.id if template else None,
        "status": "idle",
        "access_mode": "private",
        "company_access_level": "use",
    }
    if template and template.default_autonomy_policy:
        obj_in["autonomy_policy"] = template.default_autonomy_policy

    agent = await agent_dao.create(obj_in=obj_in)

    _ = await participant_dao.create(
        obj_in={
            "type": "agent",
            "ref_id": agent.id,
            "display_name": agent.name,
            "avatar_url": agent.avatar_url,
        }
    )
    _ = await agent_permission_dao.create(
        obj_in={
            "agent_id": agent.id,
            "scope_type": "user",
            "scope_id": user.id,
            "access_level": "manage",
        }
    )
    _ = await ensure_access_granted_platform_relationships(db, agent, created_by_user_id=user.id)

    from app.api.agents import _persist_agent_runtime
    from app.services.agent_manager import agent_manager

    write_gateway_api_key(agent, raw_key)
    from app.db.session import flush_request_transaction

    await flush_request_transaction()
    await agent_manager.initialize_agent_files(
        agent,
        personality=personality_note,
        boundaries=boundaries,
    )
    from app.api.relationships import _regenerate_relationships_file

    await _regenerate_relationships_file(agent.id)

    try:
        _ = await agent_manager.start_container(db, agent)
        agent = await _persist_agent_runtime(agent)
    except Exception:
        _ = await agent_dao.update(db_obj=agent, obj_in={"status": "error"})
        raise

    return agent, raw_key


@router.get("/status")
async def get_onboarding_status(current_user: UserRecord = Depends(get_current_user)):
    """Return onboarding state for the current user/company."""
    return _status_payload(await _get_row(current_user))


@router.post("/start")
async def start_onboarding(data: OnboardingStartRequest, current_user: UserRecord = Depends(get_current_user)):
    """Start or resume onboarding for the current user/company."""
    row = await _ensure_row(current_user, data.entry_mode)
    return _status_payload(row)


@router.post("/personal-assistant", status_code=status.HTTP_201_CREATED)
async def create_personal_assistant(
    data: PersonalAssistantRequest, current_user: UserRecord = Depends(get_current_user), db: object | None = None
) -> dict[str, Any]:
    """Create the user's private assistant and advance onboarding."""
    row = await _ensure_row(current_user, "join")
    if row.personal_assistant_agent_id:
        existing = await agent_dao.get(row.personal_assistant_agent_id)
        if existing:
            row = await user_tenant_onboarding_dao.update(db_obj=row, obj_in={"current_step": "opening"}) or row
            return {"agent": {"id": str(existing.id), "name": existing.name}, "onboarding": _status_payload(row)}

    agent, raw_key = await _create_personal_assistant(db, current_user, data)
    row = (
        await user_tenant_onboarding_dao.update(
            db_obj=row,
            obj_in={
                "personal_assistant_agent_id": agent.id,
                "current_step": "opening",
                "status": "in_progress",
            },
        )
        or row
    )
    return {
        "agent": {"id": str(agent.id), "name": agent.name, "api_key": raw_key},
        "onboarding": _status_payload(row),
    }


@router.post("/complete")
async def complete_onboarding(current_user: UserRecord = Depends(get_current_user)):
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
