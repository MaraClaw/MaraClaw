"""Company onboarding APIs."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.dao.onboarding_dao import user_tenant_onboarding_dao
from app.records.onboarding import UserTenantOnboardingRecord
from app.records.user import UserRecord
from app.schemas.organization_response_onboarding import OnboardingStatusPayload

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingStartRequest(BaseModel):
    entry_mode: str = Field(default="create", pattern="^(create|join)$")


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
