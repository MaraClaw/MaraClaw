from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_tenants, lock_user
from app.core.domain.agents import (
    AgentPermissionScope,
    AgentSharingMode,
    AgentStatus,
    AgentType,
    PermissionAccessLevel,
    PermissionGrantScope,
)
from app.core.security import raise_if_password_change_required
from app.dao.agent_dao import agent_dao, agent_permission_dao
from app.dao.org_department_dao import org_department_dao
from app.dao.participant_dao import participant_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.schemas import AgentCreate
from app.services.access_relationships import ensure_access_granted_platform_relationships
from app.services.openclaw_keys import mint_openclaw_gateway_key
from app.services.quota_guard import QuotaExceeded, check_agent_creation_quota


@dataclass(frozen=True, slots=True)
class AgentProvisioning:
    actor: UserRecord
    tenant_id: UUID | None
    tenant: TenantRecord | None


@asynccontextmanager
async def agent_provisioning(user: UserRecord, tenant_id: UUID | None) -> AsyncGenerator[AgentProvisioning]:
    async with connection_ctx() as db:
        await lock_tenants(db, (tenant_id, user.tenant_id))
        await lock_user(db, user.id, user.tenant_id)
        actor = await user_dao.get_with_identity(user.id, fresh=True)
        if actor is None or not actor.is_active or (actor.identity is not None and not actor.identity.is_active):
            raise HTTPException(403, "User is inactive")
        raise_if_password_change_required(actor)
        if actor.tenant_id != user.tenant_id:
            raise HTTPException(409, "User organization changed; retry")
        if tenant_id != actor.tenant_id and actor.role not in ("platform_admin", "org_admin"):
            raise HTTPException(403, "Organization access denied")
        tenant = await tenant_dao.get(tenant_id) if tenant_id else None
        if tenant is not None and not tenant.is_active:
            raise HTTPException(403, "Organization is inactive")
        yield AgentProvisioning(actor, tenant_id, tenant)


async def provision_agent(
    context: AgentProvisioning, data: AgentCreate, *, setup_pending: bool = False
) -> tuple[AgentRecord, str]:
    try:
        await check_agent_creation_quota(context.actor)
    except QuotaExceeded as exc:
        raise HTTPException(403, exc.message) from exc

    scope = data.permission_scope_type
    if scope not in ("company", "user", "custom"):
        raise HTTPException(400, "Unsupported permission_scope_type")
    user_ids = set(data.permission_scope_ids)
    if user_ids:
        users = await user_dao.get_many(list(user_ids))
        if len(users) != len(user_ids) or any(user.tenant_id != context.tenant_id for user in users):
            raise HTTPException(422, "Invalid user for this organization")
    department_ids = set(data.department_ids)
    if department_ids and scope != "custom":
        raise HTTPException(422, "Department grants require custom sharing")
    if department_ids:
        departments = await org_department_dao.get_many(list(department_ids))
        if len(departments) != len(department_ids) or any(
            department.tenant_id != context.tenant_id for department in departments
        ):
            raise HTTPException(422, "Invalid department for this organization")

    tenant = context.tenant
    ttl_hours = tenant.default_agent_ttl_hours if tenant else context.actor.quota_agent_ttl_hours
    access_level = data.permission_access_level
    match scope:
        case AgentPermissionScope.COMPANY:
            access_mode = AgentSharingMode.COMPANY
        case AgentPermissionScope.USER:
            access_mode = AgentSharingMode.PRIVATE
        case AgentPermissionScope.CUSTOM:
            access_mode = AgentSharingMode.CUSTOM
        case unreachable:
            assert_never(unreachable)
    values: dict[str, object] = {
        "name": data.name,
        "role_description": data.role_description,
        "bio": data.bio,
        "welcome_message": data.welcome_message,
        "avatar_url": data.avatar_url,
        "creator_id": context.actor.id,
        "tenant_id": context.tenant_id,
        "agent_type": AgentType.OPENCLAW,
        "gogcli_enabled": bool(data.gogcli_enabled),
        "primary_model_id": data.primary_model_id,
        "secondary_model_id": data.secondary_model_id,
        "fallback_model_id": data.fallback_model_id,
        "max_tokens_per_day": data.max_tokens_per_day,
        "max_tokens_per_month": data.max_tokens_per_month,
        "template_id": data.template_id,
        "status": AgentStatus.IDLE,
        "setup_pending": setup_pending,
        "expires_at": datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours and ttl_hours > 0 else None,
        "max_llm_calls_per_day": (tenant.default_max_llm_calls_per_day or 1000) if tenant else 1000,
        "max_triggers": (tenant.default_max_triggers or 20) if tenant else 20,
        "min_poll_interval_min": (tenant.min_poll_interval_floor or 5) if tenant else 5,
        "webhook_rate_limit": (tenant.max_webhook_rate_ceiling or 5) if tenant else 5,
        "heartbeat_interval_minutes": max(240, tenant.min_heartbeat_interval_minutes or 240) if tenant else 240,
        "access_mode": access_mode,
        "company_access_level": access_level,
    }
    if bool(data.autonomy_policy):
        values["autonomy_policy"] = data.autonomy_policy
    agent = await agent_dao.create(obj_in=values)
    _ = await participant_dao.create(
        obj_in={"type": "agent", "ref_id": agent.id, "display_name": agent.name, "avatar_url": agent.avatar_url}
    )
    if scope == "company":
        _ = await agent_permission_dao.create(
            obj_in={"agent_id": agent.id, "scope_type": PermissionGrantScope.COMPANY, "access_level": access_level}
        )
    elif scope == "user" and data.permission_scope_ids:
        for scope_id in data.permission_scope_ids:
            _ = await agent_permission_dao.create(
                obj_in={
                    "agent_id": agent.id,
                    "scope_type": PermissionGrantScope.USER,
                    "scope_id": scope_id,
                    "access_level": access_level,
                }
            )
    else:
        _ = await agent_permission_dao.create(
            obj_in={
                "agent_id": agent.id,
                "scope_type": PermissionGrantScope.USER,
                "scope_id": context.actor.id,
                "access_level": PermissionAccessLevel.MANAGE,
            }
        )
        for department_id in sorted(department_ids):
            _ = await agent_permission_dao.create(
                obj_in={
                    "agent_id": agent.id,
                    "scope_type": PermissionGrantScope.DEPARTMENT,
                    "scope_id": department_id,
                    "access_level": PermissionAccessLevel.USE,
                }
            )
    _ = await ensure_access_granted_platform_relationships(agent, created_by_user_id=context.actor.id)
    raw_key, key_hash = mint_openclaw_gateway_key()
    agent = await agent_dao.update(db_obj=agent, obj_in={"api_key_hash": key_hash})
    return agent, raw_key
