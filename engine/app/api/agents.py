"""Agent (Digital Employee) API routes."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from anyio import Path as AsyncPath, get_cancelled_exc_class
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from python_on_whales import ClientNotFoundError
from python_on_whales.exceptions import DockerException, NoSuchContainer

from app.config import get_settings
from app.core.json_types import int_from_row, json_as_int, object_mapping_from, uuid_from_row
from app.core.logging import logger
from app.core.permissions import check_agent_access, is_agent_creator, list_visible_agents
from app.core.security import get_current_user
from app.dao import agent_dao, agent_permission_dao, agent_template_dao
from app.dao.approval_dao import approval_request_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.participant_dao import participant_dao
from app.dao.skill_dao import skill_dao
from app.dao.task_dao import task_dao, task_log_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.schemas.schemas import AgentCreate, AgentOut, AgentUpdate
from app.services.access_relationships import ensure_access_granted_platform_relationships
from app.services.agent_manager import agent_manager
from app.services.gogcli_runtime import gogcli_skill_folder_names
from app.services.okr_agent_hook import hook_new_agent
from app.services.quota_guard import QuotaExceeded, check_agent_creation_quota
from app.services.resource_discovery import import_mcp_from_smithery
from app.services.storage import get_storage_backend

router = APIRouter(prefix="/agents", tags=["agents"])
settings = get_settings()

# Re-export cleanup SQL for tests (parameterized with %(aid)s).
from app.dao.agent_dao import AGENT_DELETE_CLEANUP_SQL as _DELETE_AGENT_CLEANUP_SQL  # noqa: E402

_DELETE_AGENT_CLEANUP_STATEMENTS: Final[tuple[str, ...]] = _DELETE_AGENT_CLEANUP_SQL


class AgentPermissionUserAccess(BaseModel):
    id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    access_level: str = "use"


class AgentPermissionUpdate(BaseModel):
    scope_type: str = "company"
    scope_ids: list[uuid.UUID] = Field(default_factory=list[uuid.UUID])
    user_access: list[AgentPermissionUserAccess] = Field(default_factory=list[AgentPermissionUserAccess])
    access_level: str = "use"


class AgentApprovalResolveRequest(BaseModel):
    action: str = "reject"


async def _get_active_admin_users(tenant_id: uuid.UUID | None) -> list[UserRecord]:
    if not tenant_id:
        return []
    return list(await user_dao.list_active_admins_for_tenant(tenant_id))


def _serialize_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _archive_agent_task_history(agent_id: uuid.UUID, archive_dir: Path) -> Path | None:
    """Persist task and task-log history into the agent archive directory before DB cleanup."""
    tasks = await task_dao.list_for_agent(agent_id, ascending=True)
    if not tasks:
        return None

    await AsyncPath(archive_dir).mkdir(parents=True, exist_ok=True)

    task_items: list[dict[str, object]] = []
    payload: dict[str, object] = {
        "agent_id": str(agent_id),
        "archived_at": datetime.now(UTC).isoformat(),
        "tasks": task_items,
    }

    for task in tasks:
        logs = await task_log_dao.list_for_task(task.id)
        task_items.append(
            {
                "id": str(task.id),
                "title": task.title,
                "description": task.description,
                "type": task.type,
                "status": task.status,
                "priority": task.priority,
                "assignee": task.assignee,
                "created_by": str(task.created_by),
                "due_date": _serialize_dt(task.due_date),
                "supervision_target_user_id": (
                    str(task.supervision_target_user_id) if task.supervision_target_user_id else None
                ),
                "supervision_target_name": task.supervision_target_name,
                "supervision_channel": task.supervision_channel,
                "remind_schedule": task.remind_schedule,
                "created_at": _serialize_dt(task.created_at),
                "updated_at": _serialize_dt(task.updated_at),
                "completed_at": _serialize_dt(task.completed_at),
                "logs": [
                    {
                        "id": str(log.id),
                        "content": log.content,
                        "created_at": _serialize_dt(log.created_at),
                    }
                    for log in logs
                ],
            }
        )

    archive_path = archive_dir / "task_history.json"
    _ = await AsyncPath(archive_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return archive_path


async def _lazy_reset_token_counters(agent: AgentRecord) -> bool:
    """Reset daily/monthly token counters if the day or month has changed."""
    updated = await agent_dao.apply_token_counter_resets(agent)
    if updated is None:
        return False
    agent.tokens_used_today = updated.tokens_used_today
    agent.cache_read_tokens_today = updated.cache_read_tokens_today
    agent.cache_creation_tokens_today = updated.cache_creation_tokens_today
    agent.last_daily_reset = updated.last_daily_reset
    agent.tokens_used_month = updated.tokens_used_month
    agent.cache_read_tokens_month = updated.cache_read_tokens_month
    agent.cache_creation_tokens_month = updated.cache_creation_tokens_month
    agent.last_monthly_reset = updated.last_monthly_reset
    return True


async def _build_unread_count_by_agent(
    agents: list[AgentRecord],
    current_user: UserRecord,
) -> dict[str, int]:
    """Return unread assistant/system/tool message counts for the current user per agent."""
    if not agents:
        return {}

    from app.db.session import connection_ctx

    agent_ids = [agent.id for agent in agents]
    async with connection_ctx() as conn:
        rows = await conn.fetchall(
            """
            SELECT cs.agent_id AS agent_id, COUNT(cm.id) AS cnt
            FROM chat_sessions cs
            JOIN chat_messages cm ON cm.conversation_id = cs.id::text
            WHERE cs.agent_id = ANY(%(agent_ids)s)
              AND cs.user_id = %(user_id)s
              AND cs.is_group IS FALSE
              AND cs.source_channel NOT IN ('agent', 'trigger')
              AND cm.role IN ('assistant', 'system', 'tool_call')
              AND cm.created_at > COALESCE(
                    cs.last_read_at_by_user,
                    TIMESTAMPTZ '1970-01-01 00:00:00+00'
              )
            GROUP BY cs.agent_id
            """,
            {"agent_ids": agent_ids, "user_id": current_user.id},
        )
    return {str(uuid_from_row(row["agent_id"])): int_from_row(row["cnt"]) for row in rows}


def _serialize_agent_out(agent: AgentRecord, unread_count: int = 0) -> AgentOut:
    return AgentOut.model_validate(agent).model_copy(update={"unread_count": unread_count})


async def _agent_to_out(agent: AgentRecord, viewer_id: uuid.UUID) -> AgentOut:
    """Serialize one agent with ``onboarded_for_me`` for the given viewer."""
    from app.services.onboarding import is_onboarded

    model = AgentOut.model_validate(agent)
    model.onboarded_for_me = await is_onboarded(None, agent.id, viewer_id)
    return model


async def _persist_agent_runtime(agent: AgentRecord) -> AgentRecord:
    """Persist fields agent_manager may mutate in-memory (status/container)."""
    return await agent_dao.update(
        db_obj=agent,
        obj_in={
            "status": agent.status,
            "container_id": agent.container_id,
            "container_port": agent.container_port,
            "last_active_at": agent.last_active_at,
            "api_key_hash": agent.api_key_hash,
        },
    )


@router.get("/templates")
async def list_templates(current_user: UserRecord = Depends(get_current_user)) -> list[dict[str, object]]:
    """List all available agent templates."""
    _ = current_user
    templates = await agent_template_dao.list_all_ordered()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "description": t.description,
            "icon": t.icon,
            "category": t.category,
            "is_builtin": t.is_builtin,
            "soul_template": t.soul_template,
            "default_skills": t.default_skills,
            "default_autonomy_policy": t.default_autonomy_policy,
            "capability_bullets": t.capability_bullets or [],
            "has_bootstrap": bool(t.bootstrap_content),
        }
        for t in templates
    ]


@router.get("/", response_model=list[AgentOut])
async def list_agents(
    tenant_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    current_user: UserRecord = Depends(get_current_user),
):
    """List all agents the current user has access to."""
    if tenant_id and tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only list agents in your own company",
        )

    requested_tenant_id = current_user.tenant_id
    agents = list(await list_visible_agents(current_user, tenant_id=requested_tenant_id, limit=limit))

    await agent_dao.apply_token_counter_resets_many(agents)

    unread_by_agent = await _build_unread_count_by_agent(agents, current_user)
    from app.services.onboarding import onboarded_agent_ids

    onboarded = await onboarded_agent_ids(None, current_user.id, [a.id for a in agents])
    out: list[AgentOut] = []
    for a in agents:
        model = _serialize_agent_out(a, unread_by_agent.get(str(a.id), 0))
        model.onboarded_for_me = a.id in onboarded
        out.append(model)
    return out


async def _set_agent_status_error(agent_id: uuid.UUID) -> None:
    agent = await agent_dao.get(agent_id)
    if agent:
        _ = await agent_dao.update(db_obj=agent, obj_in={"status": "error"})


async def _background_agent_setup(
    agent_id: uuid.UUID,
    personality: str,
    boundaries: str,
    skill_ids: list[uuid.UUID],
    template_skill_folder_names: list[str],
    template_mcp_servers: list[str],
) -> None:
    """Run all creation tasks asynchronously with short-lived DAO sessions."""
    try:
        agent = await agent_dao.get(agent_id)
        if not agent:
            logger.error(f"[background_agent_setup] Agent {agent_id} not found")
            return
        await agent_manager.initialize_agent_files(
            agent,
            personality=personality,
            boundaries=boundaries,
        )
    except Exception as e:
        logger.exception(f"Error during agent file initialization for {agent_id}: {e}")
        await _set_agent_status_error(agent_id)
        return

    skill_files_to_write: list[tuple[str, str]] = []
    try:
        default_ids = await skill_dao.list_default_ids()
        template_skill_ids = await skill_dao.list_ids_by_folder_names(template_skill_folder_names)
        all_skill_ids = set(skill_ids) | default_ids | template_skill_ids
        if all_skill_ids:
            skills = await skill_dao.list_with_files_by_ids(list(all_skill_ids))
            agent_prefix = agent_manager._agent_storage_prefix(agent_id)
            for skill in skills:
                skill_files_to_write.extend(
                    (f"{agent_prefix}/skills/{skill.folder_name}/{sf.path}", sf.content) for sf in skill.files
                )
    except Exception as e:
        logger.exception(f"Error resolving skills for agent {agent_id}: {e}")
        await _set_agent_status_error(agent_id)
        return

    if skill_files_to_write:
        try:
            import asyncio

            storage = get_storage_backend()
            _ = await asyncio.gather(
                *[storage.write_text(key, content, encoding="utf-8") for key, content in skill_files_to_write]
            )
            logger.info(f"[_skills_copy] background agent={agent_id} files={len(skill_files_to_write)} completed")
        except Exception as e:
            logger.exception(f"Error copying skills files for agent {agent_id}: {e}")
            await _set_agent_status_error(agent_id)
            return

    if template_mcp_servers:
        for server_id in template_mcp_servers:
            try:
                result_msg = await import_mcp_from_smithery(
                    server_id=server_id,
                    agent_id=agent_id,
                    config={},
                )
                if result_msg.startswith("❌"):
                    logger.warning(
                        f"[create_agent] background MCP pre-install for '{server_id}' "
                        + f"on agent {agent_id} reported error: {result_msg[:200]}"
                    )
                else:
                    logger.info(
                        f"[create_agent] background MCP pre-install '{server_id}' succeeded for agent {agent_id}"
                    )
            except Exception as e:
                logger.warning(
                    f"[create_agent] background MCP pre-install for '{server_id}' on agent {agent_id} raised: {e}"
                )

    try:
        agent = await agent_dao.get(agent_id)
        if not agent:
            logger.error(f"[background_agent_setup] Agent {agent_id} not found before starting container")
            return

        _ = await agent_manager.start_container(None, agent)
        _ = await _persist_agent_runtime(agent)

        if agent.tenant_id:
            await hook_new_agent(None, agent.id, agent.tenant_id)
    except Exception as e:
        logger.exception(f"Error starting container for agent {agent_id}: {e}")
        await _set_agent_status_error(agent_id)


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate, background_tasks: BackgroundTasks, current_user: UserRecord = Depends(get_current_user)
):
    """Create a new digital employee (any authenticated user)."""
    try:
        await check_agent_creation_quota(current_user.id)
    except QuotaExceeded as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message) from exc

    ttl_hours = current_user.quota_agent_ttl_hours

    target_tenant_id = current_user.tenant_id
    if current_user.role in ("platform_admin", "org_admin") and data.tenant_id:
        target_tenant_id = data.tenant_id

    max_llm_calls = 1000
    default_max_triggers = 20
    default_min_poll = 5
    default_webhook_rate = 5
    default_heartbeat_interval = 240
    tenant_default_model_id = None
    if target_tenant_id:
        tenant = await tenant_dao.get(target_tenant_id)
        if tenant:
            ttl_hours = tenant.default_agent_ttl_hours
            max_llm_calls = tenant.default_max_llm_calls_per_day or 1000
            default_max_triggers = tenant.default_max_triggers or 20
            default_min_poll = tenant.min_poll_interval_floor or 5
            default_webhook_rate = tenant.max_webhook_rate_ceiling or 5
            tenant_default_model_id = tenant.default_model_id
            if (
                tenant.min_heartbeat_interval_minutes
                and tenant.min_heartbeat_interval_minutes > default_heartbeat_interval
            ):
                default_heartbeat_interval = tenant.min_heartbeat_interval_minutes

    effective_primary_model_id = data.primary_model_id or tenant_default_model_id
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours and ttl_hours > 0 else None

    access_level = data.permission_access_level if data.permission_access_level in ("use", "manage") else "use"
    if data.permission_scope_type not in ("company", "user", "custom"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported permission_scope_type")

    if data.permission_scope_type == "company":
        access_mode = "company"
    elif data.permission_scope_type == "user":
        access_mode = "private"
    else:
        access_mode = "custom"

    async with connection_ctx():
        agent = await agent_dao.create(
            obj_in={
                "name": data.name,
                "role_description": data.role_description,
                "bio": data.bio,
                "avatar_url": data.avatar_url,
                "creator_id": current_user.id,
                "tenant_id": target_tenant_id,
                "agent_type": data.agent_type or "native",
                "gogcli_enabled": data.gogcli_enabled,
                "primary_model_id": effective_primary_model_id,
                "fallback_model_id": data.fallback_model_id,
                "max_tokens_per_day": data.max_tokens_per_day,
                "max_tokens_per_month": data.max_tokens_per_month,
                "template_id": data.template_id,
                "status": "creating" if data.agent_type != "openclaw" else "idle",
                "expires_at": expires_at,
                "max_llm_calls_per_day": max_llm_calls,
                "max_triggers": default_max_triggers,
                "min_poll_interval_min": default_min_poll,
                "webhook_rate_limit": default_webhook_rate,
                "heartbeat_interval_minutes": default_heartbeat_interval,
                "access_mode": access_mode,
                "company_access_level": access_level,
                **({"autonomy_policy": data.autonomy_policy} if data.autonomy_policy else {}),
            }
        )

        _ = await participant_dao.create(
            obj_in={
                "type": "agent",
                "ref_id": agent.id,
                "display_name": agent.name,
                "avatar_url": agent.avatar_url,
            }
        )

        if data.permission_scope_type == "company":
            _ = await agent_permission_dao.create(
                obj_in={"agent_id": agent.id, "scope_type": "company", "access_level": access_level}
            )
        elif data.permission_scope_type == "user":
            if data.permission_scope_ids:
                for scope_id in data.permission_scope_ids:
                    _ = await agent_permission_dao.create(
                        obj_in={
                            "agent_id": agent.id,
                            "scope_type": "user",
                            "scope_id": scope_id,
                            "access_level": access_level,
                        }
                    )
            else:
                _ = await agent_permission_dao.create(
                    obj_in={
                        "agent_id": agent.id,
                        "scope_type": "user",
                        "scope_id": current_user.id,
                        "access_level": "manage",
                    }
                )
        else:
            _ = await agent_permission_dao.create(
                obj_in={
                    "agent_id": agent.id,
                    "scope_type": "user",
                    "scope_id": current_user.id,
                    "access_level": "manage",
                }
            )

    _ = await ensure_access_granted_platform_relationships(None, agent, created_by_user_id=current_user.id)

    if agent.agent_type == "openclaw":
        raw_key = f"oc-{secrets.token_urlsafe(32)}"
        agent = await agent_dao.update(
            db_obj=agent,
            obj_in={
                "api_key_hash": hashlib.sha256(raw_key.encode()).hexdigest(),
                "status": "idle",
            },
        )
        if agent.tenant_id:
            await hook_new_agent(None, agent.id, agent.tenant_id)

        out_model = await _agent_to_out(agent, current_user.id)
        out = out_model.model_dump()
        out["api_key"] = raw_key
        return out

    folder_names: list[str] = []
    template_mcp_servers: list[str] = []
    if data.template_id:
        tpl = await agent_template_dao.get(data.template_id)
        if tpl:
            folder_names = list(tpl.default_skills or [])
            template_mcp_servers = list(tpl.default_mcp_servers or [])

    if data.gogcli_enabled:
        for folder_name in gogcli_skill_folder_names():
            if folder_name not in folder_names:
                folder_names.append(folder_name)

    out = await _agent_to_out(agent, current_user.id)

    background_tasks.add_task(
        _background_agent_setup,
        agent_id=agent.id,
        personality=data.personality or "",
        boundaries=data.boundaries or "",
        skill_ids=list(data.skill_ids or []),
        template_skill_folder_names=folder_names,
        template_mcp_servers=template_mcp_servers,
    )

    return out


@router.get("/{agent_id}")
async def get_agent(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Get agent details."""
    agent, access_level = await check_agent_access(current_user, agent_id)
    _ = await _lazy_reset_token_counters(agent)
    out_model = await _agent_to_out(agent, current_user.id)
    out = out_model.model_dump()
    out["access_level"] = access_level

    if agent.creator_id:
        creator = await user_dao.get_with_identity(agent.creator_id)
        out["creator_username"] = creator.username if creator else None

    effective_tz = agent.timezone
    if not effective_tz and agent.tenant_id:
        tenant = await tenant_dao.get(agent.tenant_id)
        if tenant:
            effective_tz = tenant.timezone or "UTC"
    out["effective_timezone"] = effective_tz or "UTC"

    return out


@router.get("/{agent_id}/permissions")
async def get_agent_permissions(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> dict[str, object]:
    """Get agent permission scope."""
    agent, access_level = await check_agent_access(current_user, agent_id)
    perms = list(await agent_permission_dao.list_for_agent(agent_id))
    can_manage = access_level == "manage"
    is_owner = is_agent_creator(current_user, agent)
    access_mode = agent.access_mode or "company"

    if not perms:
        return {
            "scope_type": access_mode,
            "scope_ids": [],
            "user_access": [],
            "access_level": "manage" if is_owner else "use",
            "effective_access_level": access_level,
            "can_manage": can_manage,
            "is_owner": is_owner,
            "creator_id": str(agent.creator_id) if agent.creator_id else None,
        }

    scope_type = access_mode
    scope_ids = [str(p.scope_id) for p in perms if p.scope_type == "user" and p.scope_id]
    perm_access_level = agent.company_access_level or next(
        (p.access_level for p in perms if p.scope_type == "company"),
        "use",
    )

    scope_names: list[dict[str, object]] = []
    user_access: list[dict[str, object]] = []
    display_user_ids = {uuid.UUID(sid) for sid in scope_ids}
    if access_mode == "custom":
        if agent.creator_id:
            display_user_ids.add(agent.creator_id)
        display_user_ids.update(admin.id for admin in await _get_active_admin_users(agent.tenant_id))

    if display_user_ids:
        users_by_id = await user_dao.get_many_with_identity(list(display_user_ids))
        users_by_id_str = {str(k): v for k, v in users_by_id.items()}
        access_by_user_id = {
            str(perm.scope_id): (perm.access_level or "use")
            for perm in perms
            if perm.scope_type == "user" and perm.scope_id
        }
        ordered_user_ids = [str(uid) for uid in display_user_ids]
        ordered_user_ids.sort(
            key=lambda sid: (user.display_name or user.username or "") if (user := users_by_id_str.get(sid)) else ""
        )
        for perm in perms:
            if perm.scope_type != "user" or not perm.scope_id:
                continue
            sid = str(perm.scope_id)
            if sid not in ordered_user_ids:
                ordered_user_ids.append(sid)

        for sid in ordered_user_ids:
            u = users_by_id_str.get(sid)
            if not u:
                continue
            is_creator = agent.creator_id == u.id
            is_admin = u.role in ("platform_admin", "org_admin")
            is_required = access_mode == "custom" and (is_creator or is_admin)
            item: dict[str, object] = {
                "id": sid,
                "name": u.display_name or u.username,
                "username": u.username,
                "email": u.email,
                "role": u.role,
                "access_level": "manage" if is_required else access_by_user_id.get(sid, "use"),
                "is_required": is_required,
                "required_reason": "creator" if is_creator else "company_admin" if is_admin else None,
            }
            scope_names.append({"id": sid, "name": item["name"]})
            user_access.append(item)

    return {
        "scope_type": scope_type,
        "scope_ids": scope_ids,
        "scope_names": scope_names,
        "user_access": user_access,
        "access_level": perm_access_level,
        "effective_access_level": access_level,
        "can_manage": can_manage,
        "is_owner": is_owner,
        "creator_id": str(agent.creator_id) if agent.creator_id else None,
    }


@router.put("/{agent_id}/permissions")
async def update_agent_permissions(
    agent_id: uuid.UUID, data: AgentPermissionUpdate, current_user: UserRecord = Depends(get_current_user)
):
    """Update agent permission scope (owner or platform_admin only)."""
    agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")

    scope_type = data.scope_type
    scope_ids = data.scope_ids
    user_access = data.user_access
    access_level = data.access_level
    if access_level not in ("use", "manage"):
        access_level = "use"
    if scope_type not in ("company", "user", "private", "custom"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported scope_type")
    if scope_type == "user":
        scope_type = "private"

    await agent_permission_dao.delete_for_agent(agent_id)

    if scope_type == "company":
        agent = await agent_dao.update(
            db_obj=agent,
            obj_in={"access_mode": "company", "company_access_level": access_level},
        )
        _ = await agent_permission_dao.create(
            obj_in={"agent_id": agent_id, "scope_type": "company", "access_level": access_level}
        )
    elif scope_type == "private":
        agent = await agent_dao.update(
            db_obj=agent,
            obj_in={"access_mode": "private", "company_access_level": access_level},
        )
        _ = await agent_permission_dao.create(
            obj_in={
                "agent_id": agent_id,
                "scope_type": "user",
                "scope_id": agent.creator_id or current_user.id,
                "access_level": "manage",
            }
        )
    elif scope_type == "custom":
        agent = await agent_dao.update(
            db_obj=agent,
            obj_in={"access_mode": "custom", "company_access_level": access_level},
        )
        seen_user_ids: set[uuid.UUID] = set()
        creator_id = agent.creator_id or current_user.id
        required_manager_ids = {creator_id}
        required_manager_ids.update(admin.id for admin in await _get_active_admin_users(agent.tenant_id))
        for item in user_access:
            uid = item.id or item.user_id
            if uid is None:
                continue
            if uid in seen_user_ids:
                continue
            lvl = item.access_level
            if lvl not in ("use", "manage"):
                lvl = "use"
            if uid in required_manager_ids:
                lvl = "manage"
            seen_user_ids.add(uid)
            _ = await agent_permission_dao.create(
                obj_in={"agent_id": agent_id, "scope_type": "user", "scope_id": uid, "access_level": lvl}
            )
        for sid in scope_ids:
            if sid not in seen_user_ids:
                seen_user_ids.add(sid)
                _ = await agent_permission_dao.create(
                    obj_in={
                        "agent_id": agent_id,
                        "scope_type": "user",
                        "scope_id": sid,
                        "access_level": "manage" if sid in required_manager_ids else access_level,
                    }
                )
        for uid in required_manager_ids:
            if uid not in seen_user_ids:
                _ = await agent_permission_dao.create(
                    obj_in={"agent_id": agent_id, "scope_type": "user", "scope_id": uid, "access_level": "manage"}
                )

    relationships_changed = await ensure_access_granted_platform_relationships(
        None,
        agent,
        created_by_user_id=current_user.id,
    )
    if relationships_changed:
        from app.api.relationships import _regenerate_relationships_file

        await _regenerate_relationships_file(agent_id)

    return {"status": "ok"}


@router.get("/{agent_id}/permissions/candidates")
async def get_agent_permission_candidates(
    agent_id: uuid.UUID, search: str | None = None, current_user: UserRecord = Depends(get_current_user)
) -> dict[str, list[dict[str, object]]]:
    """Return org members that can be granted custom access."""
    from app.services.channel_user_service import get_platform_user_by_org_member

    agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")

    if agent.tenant_id is None:
        return {"candidates": []}
    members = list(await org_member_dao.list_permission_candidates(tenant_id=agent.tenant_id, search=search, limit=50))

    linked_user_ids = [m.user_id for m in members if m.user_id]
    users_by_id = await user_dao.get_many_with_identity(linked_user_ids)
    # Restrict to same tenant
    users_by_id = {
        uid: u for uid, u in users_by_id.items() if agent.tenant_id is None or u.tenant_id == agent.tenant_id
    }

    candidates: list[dict[str, object]] = []
    for m in members:
        if m.user_id:
            u = users_by_id.get(m.user_id)
        else:
            try:
                u = await get_platform_user_by_org_member(m, agent_tenant_id=agent.tenant_id)
            except Exception:
                logger.exception("Unable to create a platform user for organization member {}", m.id)
                continue

        if u is None:
            continue

        candidates.append(
            {
                "id": str(u.id),
                "name": m.name,
                "username": u.username if u else None,
                "email": m.email or (u.email if u else None),
                "title": m.title or None,
                "avatar_url": m.avatar_url or None,
            }
        )

    return {
        "users": candidates,
        "agents": [],
    }


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(agent_id: uuid.UUID, data: AgentUpdate, current_user: UserRecord = Depends(get_current_user)):
    """Update agent settings (creator or admin)."""
    agent, _access = await check_agent_access(current_user, agent_id)

    is_admin = current_user.role in ("platform_admin", "org_admin")

    if not is_agent_creator(current_user, agent) and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can update agent settings"
        )

    update_data = object_mapping_from(data.model_dump(exclude_unset=True))

    if "expires_at" in update_data:
        if not is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin can modify agent expiry time")
        new_expires = update_data["expires_at"]
        if (
            new_expires is None or (isinstance(new_expires, datetime) and new_expires > datetime.now(UTC))
        ) and agent.is_expired:
            update_data["is_expired"] = False
            update_data["status"] = "idle"

    clamped_fields = []
    tenant = None
    if current_user.tenant_id and (
        "heartbeat_interval_minutes" in update_data
        or {"min_poll_interval_min", "webhook_rate_limit", "max_triggers"} & set(update_data.keys())
    ):
        tenant = await tenant_dao.get(current_user.tenant_id)

    if "heartbeat_interval_minutes" in update_data and tenant:
        original = json_as_int(update_data["heartbeat_interval_minutes"])
        if original >= tenant.min_heartbeat_interval_minutes:
            original = None
    else:
        original = None
    if original is not None and tenant:
        update_data["heartbeat_interval_minutes"] = tenant.min_heartbeat_interval_minutes
        clamped_fields.append(
            {
                "field": "heartbeat_interval_minutes",
                "requested": original,
                "applied": tenant.min_heartbeat_interval_minutes,
                "reason": "company_floor",
            }
        )

    if tenant and {"min_poll_interval_min", "webhook_rate_limit", "max_triggers"} & set(update_data.keys()):
        if "min_poll_interval_min" in update_data:
            original = json_as_int(update_data["min_poll_interval_min"])
            update_data["min_poll_interval_min"] = max(original, tenant.min_poll_interval_floor)
            if update_data["min_poll_interval_min"] != original:
                clamped_fields.append(
                    {
                        "field": "min_poll_interval_min",
                        "requested": original,
                        "applied": update_data["min_poll_interval_min"],
                        "reason": "company_floor",
                    }
                )
        if "webhook_rate_limit" in update_data:
            original = json_as_int(update_data["webhook_rate_limit"])
            update_data["webhook_rate_limit"] = min(original, tenant.max_webhook_rate_ceiling)
            if update_data["webhook_rate_limit"] != original:
                clamped_fields.append(
                    {
                        "field": "webhook_rate_limit",
                        "requested": original,
                        "applied": update_data["webhook_rate_limit"],
                        "reason": "company_ceiling",
                    }
                )

    agent = await agent_dao.update(db_obj=agent, obj_in=update_data)

    if "name" in update_data or "avatar_url" in update_data:
        p = await participant_dao.get_by_type_ref("agent", agent_id)
        if p:
            p_updates: dict[str, object] = {}
            if "name" in update_data:
                p_updates["display_name"] = agent.name
            if "avatar_url" in update_data:
                p_updates["avatar_url"] = agent.avatar_url
            if p_updates:
                _ = await participant_dao.update(db_obj=p, obj_in=p_updates)

    out_model = await _agent_to_out(agent, current_user.id)
    out = out_model.model_dump()
    if clamped_fields:
        out["_clamped_fields"] = clamped_fields
    return out


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Delete a digital employee (creator only)."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in (
        "super_admin",
        "org_admin",
        "platform_admin",
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can delete agent")

    if agent.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System agents cannot be deleted. Disable the related feature (e.g. OKR) in Company Settings instead.",
        )

    archive_dir: Path | None = None
    try:
        _ = await agent_manager.remove_container(agent)
    except get_cancelled_exc_class():
        raise
    except ClientNotFoundError, DockerException, NoSuchContainer:
        pass
    try:
        archive_dir = await agent_manager.archive_agent_files(agent.id)
    except get_cancelled_exc_class():
        raise
    except OSError, shutil.Error:
        pass
    if archive_dir is not None:
        try:
            _ = await _archive_agent_task_history(agent.id, archive_dir)
        except get_cancelled_exc_class():
            raise
        except OSError:
            pass

    await agent_dao.delete_with_related(agent_id)


@router.post("/{agent_id}/start", response_model=AgentOut)
async def start_agent(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Start an agent's container."""
    agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can start agent")

    _ = await agent_manager.start_container(None, agent)
    agent = await _persist_agent_runtime(agent)
    return await _agent_to_out(agent, current_user.id)


@router.post("/{agent_id}/stop", response_model=AgentOut)
async def stop_agent(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Stop an agent's container."""
    agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can stop agent")

    _ = await agent_manager.stop_container(agent)
    agent = await _persist_agent_runtime(agent)
    return await _agent_to_out(agent, current_user.id)


@router.get("/{agent_id}/approvals")
async def list_agent_approvals(
    agent_id: uuid.UUID, status_filter: str | None = None, current_user: UserRecord = Depends(get_current_user)
) -> list[dict[str, object]]:
    """List approval requests for a specific agent. Only creator or admin can view."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only agent creator or admin can view approvals"
        )

    approvals = await approval_request_dao.list_for_agent(agent_id, status=status_filter)
    return [
        {
            "id": str(a.id),
            "agent_id": str(a.agent_id),
            "action_type": a.action_type,
            "details": a.details,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            "resolved_by": str(a.resolved_by) if a.resolved_by else None,
        }
        for a in approvals
    ]


@router.post("/{agent_id}/approvals/{approval_id}/resolve")
async def resolve_agent_approval(
    agent_id: uuid.UUID,
    approval_id: uuid.UUID,
    data: AgentApprovalResolveRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> dict[str, object]:
    """Approve or reject a pending approval for a specific agent."""
    _ = await check_agent_access(current_user, agent_id)

    from app.services.autonomy_service import autonomy_service

    try:
        approval = await autonomy_service.resolve_approval(None, approval_id, current_user, data.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "id": str(approval.id),
        "status": approval.status,
        "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else None,
    }


@router.post("/{agent_id}/api-key")
async def generate_or_reset_api_key(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Generate or regenerate API key for an OpenClaw agent."""
    agent, _access = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can manage API keys")
    if agent.agent_type != "openclaw":
        raise HTTPException(status_code=400, detail="API keys are only available for OpenClaw agents")

    raw_key = f"oc-{secrets.token_urlsafe(32)}"
    _ = await agent_dao.update(
        db_obj=agent,
        obj_in={"api_key_hash": hashlib.sha256(raw_key.encode()).hexdigest()},
    )

    return {"api_key": raw_key, "message": "Key configured successfully."}


@router.get("/{agent_id}/gateway-messages")
async def list_gateway_messages(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> list[dict[str, object]]:
    """List recent gateway messages for an OpenClaw agent."""
    _ = await check_agent_access(current_user, agent_id)

    messages = await gateway_message_dao.list_recent(agent_id, limit=50)
    sender_ids = {m.sender_agent_id for m in messages if m.sender_agent_id}
    names = await agent_dao.names_for_ids(list(sender_ids))

    return [
        {
            "id": str(m.id),
            "sender_agent_name": names.get(m.sender_agent_id) if m.sender_agent_id else None,
            "content": m.content,
            "status": m.status,
            "result": m.result,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "delivered_at": m.delivered_at.isoformat() if m.delivered_at else None,
            "completed_at": m.completed_at.isoformat() if m.completed_at else None,
        }
        for m in messages
    ]
