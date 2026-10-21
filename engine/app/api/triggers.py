"""Triggers REST API - CRUD endpoints for the Aware page frontend."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import get_current_user
from app.core.json_types import JsonObject, json_as_str, json_object_from, object_mapping_from
from app.core.permissions import check_agent_access
from app.dao.trigger_dao import agent_trigger_dao
from app.records.user import UserRecord
from app.schemas.content_response_catalog import OkAcknowledgement

router = APIRouter(prefix="/api/agents", tags=["triggers"])


class TriggerResponse(BaseModel):
    id: str
    name: str
    type: str
    config: JsonObject
    reason: str
    focus_ref: str | None = None
    is_enabled: bool
    is_system: bool = False
    fire_count: int
    max_fires: int | None = None
    cooldown_seconds: int
    last_fired_at: str | None = None
    created_at: str | None = None
    expires_at: str | None = None


class TriggerUpdate(BaseModel):
    config: JsonObject | None = None
    reason: str | None = None
    is_enabled: bool | None = None
    max_fires: int | None = None
    cooldown_seconds: int | None = None
    expires_at: str | None = None


def _can_manage(user: UserRecord, access_level: str) -> bool:
    return access_level == "manage" or user.role in ("platform_admin", "org_admin")


@router.get("/{agent_id}/triggers", response_model=list[TriggerResponse])
async def list_agent_triggers(
    agent_id: uuid.UUID, user: UserRecord = Depends(get_current_user)
) -> list[TriggerResponse]:
    """List all triggers for an agent."""
    _, access_level = await check_agent_access(user, agent_id)
    can_manage = _can_manage(user, access_level)
    triggers = await agent_trigger_dao.list_for_agent(agent_id)

    return [
        TriggerResponse(
            id=str(t.id),
            name=t.name,
            type=t.type,
            config={
                key: value
                for key, value in json_object_from(t.config).items()
                if can_manage or key not in ("token", "secret")
            },
            reason=t.reason or "",
            focus_ref=t.focus_ref,
            is_enabled=t.is_enabled,
            is_system=t.is_system,
            fire_count=t.fire_count,
            max_fires=t.max_fires,
            cooldown_seconds=t.cooldown_seconds,
            last_fired_at=t.last_fired_at.isoformat() if t.last_fired_at else None,
            created_at=t.created_at.isoformat() if t.created_at else None,
            expires_at=t.expires_at.isoformat() if t.expires_at else None,
        )
        for t in triggers
    ]


@router.patch("/{agent_id}/triggers/{trigger_id}", response_model=None)
async def update_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    body: TriggerUpdate,
    user: UserRecord = Depends(get_current_user),
) -> OkAcknowledgement:
    """Update a trigger (from frontend management UI)."""
    _, access_level = await check_agent_access(user, agent_id)
    if not _can_manage(user, access_level):
        raise HTTPException(403, "Manage access required")
    trigger = await agent_trigger_dao.get_for_agent(trigger_id, agent_id)
    if not trigger:
        raise HTTPException(404, "Trigger not found")

    updates = object_mapping_from(body.model_dump(exclude_unset=True))
    if "config" in updates:
        config = dict(json_object_from(trigger.config))
        config.update({key: value for key, value in (body.config or {}).items() if key not in ("token", "secret")})
        updates["config"] = config
    expires_at = updates.get("expires_at")
    if expires_at is not None:
        if isinstance(expires_at, datetime):
            updates["expires_at"] = expires_at
        else:
            expires_text = json_as_str(expires_at)
            if expires_text is not None:
                updates["expires_at"] = datetime.fromisoformat(expires_text)
    if updates:
        _ = await agent_trigger_dao.update(db_obj=trigger, obj_in=updates)

    return {"ok": True}


@router.delete("/{agent_id}/triggers/{trigger_id}", response_model=None)
async def delete_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    user: UserRecord = Depends(get_current_user),
) -> OkAcknowledgement:
    """Delete a trigger entirely."""
    _, access_level = await check_agent_access(user, agent_id)
    if not _can_manage(user, access_level):
        raise HTTPException(403, "Manage access required")
    trigger = await agent_trigger_dao.get_for_agent(trigger_id, agent_id)
    if not trigger:
        raise HTTPException(404, "Trigger not found")

    _ = await agent_trigger_dao.delete(id=trigger.id)
    return {"ok": True}
