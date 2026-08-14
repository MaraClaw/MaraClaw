"""Agent collaboration and template market API routes."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ConfigDict

from app.core.json_types import JsonObject
from app.core.permissions import check_agent_access
from app.core.security import get_current_admin, get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.task_dao import task_dao
from app.dao.template_dao import agent_template_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.template import AgentTemplateRecord
from app.records.user import UserRecord
from app.services.audit_logger import write_audit_log
from app.services.collaboration import collaboration_service

router = APIRouter(tags=["advanced"])


# ─── Collaboration ──────────────────────────────────────


class DelegateRequest(BaseModel):
    to_agent_id: uuid.UUID
    task_title: str
    task_description: str = ""


class InterAgentMessage(BaseModel):
    to_agent_id: uuid.UUID
    message: str
    msg_type: str = "notify"  # notify | consult


@router.get("/agents/{agent_id}/collaborators")
async def list_collaborators(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user), db: object | None = None
):
    """List agents that can collaborate with this agent."""
    _ = await check_agent_access(current_user, agent_id)
    return await collaboration_service.list_collaborators(db, agent_id)


@router.post("/agents/{agent_id}/collaborate/delegate")
async def delegate_task(
    agent_id: uuid.UUID, data: DelegateRequest, current_user: UserRecord = Depends(get_current_user), db: object | None = None
):
    """Delegate a task from one agent to another."""
    _ = await check_agent_access(current_user, agent_id)
    try:
        return await collaboration_service.delegate_task(
            db, agent_id, data.to_agent_id, data.task_title, data.task_description
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/agents/{agent_id}/collaborate/message")
async def send_inter_agent_message(
    agent_id: uuid.UUID, data: InterAgentMessage, current_user: UserRecord = Depends(get_current_user), db: object | None = None
):
    """Send a message between agents."""
    _ = await check_agent_access(current_user, agent_id)
    return await collaboration_service.send_message_between_agents(
        db, agent_id, data.to_agent_id, data.message, data.msg_type
    )


# ─── Template Market ────────────────────────────────────


class TemplateCreate(BaseModel):
    name: str
    description: str = ""
    icon: str = "🤖"
    category: str = "general"
    soul_template: str = ""
    default_skills: list[str] = Field(default_factory=list)
    default_autonomy_policy: JsonObject = Field(default_factory=dict)


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    icon: str
    category: str
    soul_template: str
    default_skills: list[str]
    default_autonomy_policy: JsonObject
    is_builtin: bool
    created_at: str | None = None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)


def _template_out(t: AgentTemplateRecord) -> TemplateOut:
    return TemplateOut(
        id=t.id,
        name=t.name,
        description=t.description,
        icon=t.icon,
        category=t.category,
        soul_template=t.soul_template,
        default_skills=list(t.default_skills or []),
        default_autonomy_policy=dict(t.default_autonomy_policy or {}),
        is_builtin=t.is_builtin,
        created_at=t.created_at.isoformat() if t.created_at else None,
    )


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(category: str | None = None):
    """List available agent templates."""
    templates = await agent_template_dao.list_ordered_by_name(category=category)
    return [_template_out(t) for t in templates]


@router.get("/templates/{template_id}", response_model=TemplateOut)
async def get_template(template_id: uuid.UUID):
    """Get template details."""
    template = await agent_template_dao.get(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_out(template)


@router.post("/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(data: TemplateCreate, current_user: UserRecord = Depends(get_current_user)):
    """Create a new agent template (share to template market)."""
    template = await agent_template_dao.create(
        obj_in={
            "name": data.name,
            "description": data.description,
            "icon": data.icon,
            "category": data.category,
            "soul_template": data.soul_template,
            "default_skills": data.default_skills,
            "default_autonomy_policy": data.default_autonomy_policy,
            "created_by": current_user.id,
            "is_builtin": False,
        }
    )
    return _template_out(template)


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(template_id: uuid.UUID, current_user: UserRecord = Depends(get_current_admin)):
    """Delete a template (admin or creator)."""
    _ = current_user
    template = await agent_template_dao.get(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    _ = await agent_template_dao.delete(id=template.id)


# ─── Agent Handover ─────────────────────────────────────


class HandoverRequest(BaseModel):
    new_creator_id: uuid.UUID


@router.post("/agents/{agent_id}/handover")
async def handover_agent(
    agent_id: uuid.UUID, data: HandoverRequest, current_user: UserRecord = Depends(get_current_user)
) -> dict[str, Any]:
    """Transfer ownership of a digital employee to another user."""
    from app.core.permissions import is_agent_creator

    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can handover agent")

    new_creator = await user_dao.get(data.new_creator_id)
    if not new_creator:
        raise HTTPException(status_code=404, detail="Target user not found")

    old_creator_id = agent.creator_id
    _ = await agent_dao.update(db_obj=agent, obj_in={"creator_id": data.new_creator_id})

    await write_audit_log(
        "agent:handover",
        {
            "from_creator": str(old_creator_id),
            "to_creator": str(data.new_creator_id),
        },
        agent_id=agent_id,
        user_id=current_user.id,
    )

    return {
        "status": "transferred",
        "agent_name": agent.name,
        "new_creator": new_creator.display_name,
    }


# ─── Observability ──────────────────────────────────────


@router.get("/agents/{agent_id}/metrics")
async def get_agent_metrics(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    """Get observability metrics for an agent."""
    agent, _access = await check_agent_access(current_user, agent_id)

    # Task stats
    _total_tasks = await task_dao.count_for_agent(agent_id)
    _done_tasks = await task_dao.count_for_agent(agent_id, status="done")
    _pending_tasks = await task_dao.count_for_agent(agent_id, status="pending")

    # Approval + audit stats via raw SQL (no dedicated DAO yet)
    async with connection_ctx() as conn:
        _total_approvals = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM approval_requests WHERE agent_id = %(agent_id)s",
                {"agent_id": agent_id},
            )
            or 0
        )
        _pending_approvals = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM approval_requests WHERE agent_id = %(agent_id)s AND status = 'pending'",
                {"agent_id": agent_id},
            )
            or 0
        )
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        _recent_actions = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM audit_logs WHERE agent_id = %(agent_id)s AND created_at >= %(cutoff)s",
                {"agent_id": agent_id, "cutoff": cutoff},
            )
            or 0
        )

    # Container status
    from app.services.agent_manager import agent_manager

    container_status = agent_manager.get_container_status(agent)

    today_tokens = agent.tokens_used_today or 0
    month_tokens = agent.tokens_used_month or 0
    total_tokens = agent.tokens_used_total or 0
    completion_rate = 0.0 if not _total_tasks else round(_done_tasks / _total_tasks * 100, 1)

    return {
        "agent_id": str(agent_id),
        "agent_name": agent.name,
        "status": agent.status,
        "container": container_status,
        "tokens": {
            "used_today": agent.tokens_used_today,
            "used_month": agent.tokens_used_month,
            "used_total": agent.tokens_used_total,
            "cache_read_today": agent.cache_read_tokens_today,
            "cache_read_month": agent.cache_read_tokens_month,
            "cache_read_total": agent.cache_read_tokens_total,
            "cache_creation_today": agent.cache_creation_tokens_today,
            "cache_creation_month": agent.cache_creation_tokens_month,
            "cache_creation_total": agent.cache_creation_tokens_total,
            "cache_hit_rate_today": 0.0
            if not today_tokens
            else round((agent.cache_read_tokens_today or 0) / today_tokens, 4),
            "cache_hit_rate_month": 0.0
            if not month_tokens
            else round((agent.cache_read_tokens_month or 0) / month_tokens, 4),
            "cache_hit_rate_total": 0.0
            if not total_tokens
            else round((agent.cache_read_tokens_total or 0) / total_tokens, 4),
            "limit_day": agent.max_tokens_per_day,
            "limit_month": agent.max_tokens_per_month,
        },
        "tasks": {
            "total": _total_tasks,
            "done": _done_tasks,
            "pending": _pending_tasks,
            "completion_rate": completion_rate,
        },
        "approvals": {
            "total": _total_approvals,
            "pending": _pending_approvals,
        },
        "activity": {
            "actions_last_24h": _recent_actions,
        },
    }
