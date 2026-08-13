"""Triggers REST API - CRUD endpoints for the Aware page frontend."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import get_current_user
from app.core.json_types import JsonObject
from app.dao.trigger_dao import agent_trigger_dao

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


@router.get("/{agent_id}/triggers", response_model=list[TriggerResponse])
async def list_agent_triggers(agent_id: uuid.UUID, user=Depends(get_current_user)):
    """List all triggers for an agent."""
    _ = user
    triggers = await agent_trigger_dao.list_for_agent(agent_id)

    return [
        TriggerResponse(
            id=str(t.id),
            name=t.name,
            type=t.type,
            config=t.config or {},
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


@router.patch("/{agent_id}/triggers/{trigger_id}")
async def update_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    body: TriggerUpdate,
    user=Depends(get_current_user),
):
    """Update a trigger (from frontend management UI)."""
    _ = user
    trigger = await agent_trigger_dao.get_for_agent(trigger_id, agent_id)
    if not trigger:
        raise HTTPException(404, "Trigger not found")

    updates = body.model_dump(exclude_unset=True)
    if "expires_at" in updates and updates["expires_at"] is not None:
        updates["expires_at"] = datetime.fromisoformat(updates["expires_at"])
    if updates:
        await agent_trigger_dao.update(db_obj=trigger, obj_in=updates)

    return {"ok": True}


@router.delete("/{agent_id}/triggers/{trigger_id}")
async def delete_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Delete a trigger entirely."""
    _ = user
    trigger = await agent_trigger_dao.get_for_agent(trigger_id, agent_id)
    if not trigger:
        raise HTTPException(404, "Trigger not found")

    await agent_trigger_dao.delete(id=trigger.id)
    return {"ok": True}
