"""Enterprise management API routes: LLM pool, enterprise info, approvals, audit logs."""

from __future__ import annotations

import secrets
import string
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.json_types import (
    JsonObject,
    int_from_row,
    json_as_int,
    json_as_str_or,
    json_object_from,
    object_mapping_from,
    uuid_from_row,
    uuid_from_row_opt,
)
from app.core.logging import logger
from app.core.security import encrypt_data, get_current_admin, get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.approval_dao import approval_request_dao
from app.dao.audit_log_dao import audit_log_dao
from app.dao.enterprise_info_dao import enterprise_info_dao
from app.dao.identity_dao import identity_dao
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.invitation_code_dao import invitation_code_dao
from app.dao.llm_dao import llm_model_dao
from app.dao.org_department_dao import org_department_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.system_setting_dao import system_setting_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.records.identity import IdentityProviderRecord
from app.records.user import UserRecord
from app.schemas.schemas import (
    ApprovalAction,
    ApprovalRequestOut,
    AuditLogOut,
    EnterpriseInfoOut,
    EnterpriseInfoUpdate,
    IdentityProviderOut,
    LLMModelCreate,
    LLMModelOut,
    LLMModelUpdate,
    UserInviteRequest,
)
from app.services.autonomy_service import autonomy_service
from app.services.enterprise_sync import enterprise_sync_service
from app.services.llm import LLMMessage, create_llm_client, get_model_api_key, get_provider_manifest
from app.services.org_sync_adapter import derive_member_department_paths
from app.services.platform_service import platform_service
from app.services.sso_service import sso_service

router = APIRouter(prefix="/enterprise", tags=["enterprise"])
settings = get_settings()


def _is_platform_admin_user(user: UserRecord) -> bool:
    """Return true for tenant-role or identity-level platform admins."""
    return user.role == "platform_admin" or bool(getattr(getattr(user, "identity", None), "is_platform_admin", False))


# ─── Public: Check Email Exists ────────────────────────


class CheckEmailRequest(BaseModel):
    email: str


@router.post("/check-email-exists")
async def check_email_exists(data: CheckEmailRequest):
    """Public endpoint - check if an email address is already registered on this platform."""
    exists = await identity_dao.get_by_email(data.email.strip().lower()) is not None
    return {"exists": exists}


@router.get("/llm-providers")
async def list_llm_providers(
    current_user: UserRecord = Depends(get_current_user),
):
    """List supported LLM providers and capabilities from registry."""
    _ = current_user
    return get_provider_manifest()


class LLMTestRequest(BaseModel):
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    model_id: str | None = None


async def _load_llm_test_api_key(model_id: str | None) -> str | None:
    if not model_id:
        return None
    try:
        mid = uuid.UUID(model_id)
    except ValueError:
        return None
    existing = await llm_model_dao.get(mid)
    return get_model_api_key(existing) if existing else None


@router.post("/llm-test")
async def probe_llm_model(
    data: LLMTestRequest,
    current_user: UserRecord = Depends(get_current_admin),
) -> dict[str, Any]:
    """Test an LLM model configuration by making a simple API call."""
    import time

    _ = current_user
    api_key = data.api_key if data.api_key and not data.api_key.startswith("****") else None
    if not api_key and data.model_id:
        api_key = await _load_llm_test_api_key(data.model_id)
    if not api_key:
        return {"success": False, "latency_ms": 0, "error": "API Key is required"}

    start = time.time()
    try:
        client = create_llm_client(
            provider=data.provider,
            model=data.model,
            api_key=api_key,
            base_url=data.base_url or None,
        )
        response = await client.complete(
            messages=[LLMMessage(role="user", content="Say 'ok' and nothing else.")],
            max_tokens=16,
        )
        latency_ms = int((time.time() - start) * 1000)
        reply = (response.content or "")[:100] if response else ""
        return {"success": True, "latency_ms": latency_ms, "reply": reply}
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        return {"success": False, "latency_ms": latency_ms, "error": str(e)[:500]}


@router.get("/llm-models", response_model=list[LLMModelOut])
async def list_llm_models(
    tenant_id: str | None = None, current_user: UserRecord = Depends(get_current_user)
) -> list[LLMModelOut]:
    """List LLM models scoped to the selected tenant."""
    if tenant_id and current_user.role != "platform_admin" and str(current_user.tenant_id) != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot access other tenant's models")

    tid: uuid.UUID | None = None
    if tenant_id:
        tid = uuid.UUID(tenant_id)
    elif current_user.tenant_id:
        tid = current_user.tenant_id

    models_out = []
    for m in await llm_model_dao.list_for_tenant(tid):
        out = LLMModelOut.model_validate(m)
        key = get_model_api_key(m)
        out.api_key_masked = f"****{key[-4:]}" if len(key) > 4 else "****"
        models_out.append(out)
    return models_out


@router.post("/llm-models", response_model=LLMModelOut, status_code=status.HTTP_201_CREATED)
async def add_llm_model(
    data: LLMModelCreate, tenant_id: str | None = None, current_user: UserRecord = Depends(get_current_admin)
):
    """Add a new LLM model to the tenant's pool (admin)."""
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    model = await llm_model_dao.create(
        obj_in={
            "provider": data.provider,
            "model": data.model,
            "api_key_encrypted": encrypt_data(data.api_key, settings.SECRET_KEY),
            "base_url": data.base_url,
            "label": data.label,
            "temperature": data.temperature,
            "max_tokens_per_day": data.max_tokens_per_day,
            "enabled": data.enabled,
            "supports_vision": data.supports_vision,
            "max_output_tokens": data.max_output_tokens,
            "request_timeout": data.request_timeout,
            "tenant_id": uuid.UUID(tid) if tid else None,
        }
    )

    if model.tenant_id and model.enabled:
        tenant = await tenant_dao.get(model.tenant_id)
        if tenant and tenant.default_model_id is None:
            _ = await tenant_dao.update(db_obj=tenant, obj_in={"default_model_id": model.id})

    return LLMModelOut.model_validate(model)


@router.post("/llm-models/{model_id}/set-default", status_code=status.HTTP_204_NO_CONTENT)
async def set_default_llm_model(model_id: uuid.UUID, current_user: UserRecord = Depends(get_current_admin)):
    """Mark this model as the tenant's default for new agents."""
    _ = current_user
    model = await llm_model_dao.get(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not model.tenant_id:
        raise HTTPException(status_code=400, detail="Model is not tenant-scoped")
    if not model.enabled:
        raise HTTPException(status_code=400, detail="Model is disabled")

    tenant = await tenant_dao.get(model.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    previous_default = tenant.default_model_id
    _ = await tenant_dao.update(db_obj=tenant, obj_in={"default_model_id": model.id})

    if previous_default and previous_default != model.id:
        _ = await agent_dao.migrate_primary_model(
            tenant_id=tenant.id,
            old_model_id=previous_default,
            new_model_id=model.id,
        )
        logger.info(
            f"[set_default_llm_model] Migrated agents in tenant {tenant.id} from {previous_default} -> {model.id}"
        )


@router.delete("/llm-models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_llm_model(
    model_id: uuid.UUID, force: bool = False, current_user: UserRecord = Depends(get_current_admin)
):
    """Remove an LLM model from the pool."""
    _ = current_user
    model = await llm_model_dao.get(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    agent_names = await agent_dao.names_referencing_model(model_id)
    if agent_names and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"This model is used by {len(agent_names)} agent(s)",
                "agents": agent_names,
            },
        )

    if agent_names:
        await agent_dao.nullify_model_references(model_id)
    _ = await llm_model_dao.delete(id=model_id)


@router.put("/llm-models/{model_id}", response_model=LLMModelOut)
async def update_llm_model(
    model_id: uuid.UUID, data: LLMModelUpdate, current_user: UserRecord = Depends(get_current_admin)
):
    """Update an existing LLM model in the pool (admin)."""
    _ = current_user
    model = await llm_model_dao.get(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    updates: dict[str, Any] = {}
    if data.provider:
        updates["provider"] = data.provider
    if data.model:
        updates["model"] = data.model
    if data.label is not None:
        updates["label"] = data.label
    if hasattr(data, "base_url") and data.base_url is not None:
        updates["base_url"] = data.base_url
    if data.api_key and data.api_key.strip() and not data.api_key.startswith("****"):
        updates["api_key_encrypted"] = encrypt_data(data.api_key.strip(), settings.SECRET_KEY)
    if data.temperature is not None:
        updates["temperature"] = data.temperature
    if data.max_tokens_per_day is not None:
        updates["max_tokens_per_day"] = data.max_tokens_per_day
    if data.enabled is not None:
        updates["enabled"] = data.enabled
    if hasattr(data, "supports_vision") and data.supports_vision is not None:
        updates["supports_vision"] = data.supports_vision
    if hasattr(data, "max_output_tokens") and data.max_output_tokens is not None:
        updates["max_output_tokens"] = data.max_output_tokens
    if hasattr(data, "request_timeout") and data.request_timeout is not None:
        updates["request_timeout"] = data.request_timeout

    try:
        model = await llm_model_dao.update(db_obj=model, obj_in=updates)
        return LLMModelOut.model_validate(model)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to update model") from None


# ─── Enterprise Info ────────────────────────────────────


@router.get("/info", response_model=list[EnterpriseInfoOut])
async def list_enterprise_info(current_user: UserRecord = Depends(get_current_user)):
    """List all enterprise information entries."""
    _ = current_user
    entries = await enterprise_info_dao.list_all()
    # Stable order by info_type
    entries = sorted(entries, key=lambda e: e.info_type)
    return [EnterpriseInfoOut.model_validate(e) for e in entries]


@router.put("/info/{info_type}", response_model=EnterpriseInfoOut)
async def update_enterprise_info(
    info_type: str, data: EnterpriseInfoUpdate, current_user: UserRecord = Depends(get_current_admin)
):
    """Create or update enterprise information. Triggers sync to agents."""
    info = await enterprise_sync_service.update_enterprise_info(
        None, info_type, data.content, data.visible_roles, current_user.id
    )
    _ = await enterprise_sync_service.sync_to_all_agents(None)
    return EnterpriseInfoOut.model_validate(info)


# ─── Approvals ──────────────────────────────────────────


@router.get("/approvals", response_model=list[ApprovalRequestOut])
async def list_approvals(
    tenant_id: str | None = None, status_filter: str | None = None, current_user: UserRecord = Depends(get_current_user)
) -> list[ApprovalRequestOut]:
    """List approval requests scoped to a tenant."""
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    tenant_uuid = uuid.UUID(tid) if tid else None
    creator_id = None if current_user.role == "platform_admin" else current_user.id

    approvals = await approval_request_dao.list_scoped(
        tenant_id=tenant_uuid,
        creator_id=creator_id,
        status=status_filter,
    )

    agent_names = await agent_dao.names_for_ids(list({a.agent_id for a in approvals}))
    out: list[ApprovalRequestOut] = []
    for a in approvals:
        d = ApprovalRequestOut.model_validate(a)
        d.agent_name = agent_names.get(a.agent_id)
        out.append(d)
    return out


@router.post("/approvals/{approval_id}/resolve", response_model=ApprovalRequestOut)
async def resolve_approval(
    approval_id: uuid.UUID, data: ApprovalAction, current_user: UserRecord = Depends(get_current_user)
):
    """Approve or reject a pending approval request."""
    try:
        approval = await autonomy_service.resolve_approval(None, approval_id, current_user, data.action)
        return ApprovalRequestOut.model_validate(approval)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ─── Audit Logs ─────────────────────────────────────────


@router.get("/audit-logs", response_model=list[AuditLogOut])
async def list_audit_logs(
    agent_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
    limit: int = 50,
    current_user: UserRecord = Depends(get_current_admin),
):
    """List audit logs scoped to a tenant (admin only)."""
    _ = current_user
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    logs = await audit_log_dao.list_scoped(
        tenant_id=uuid.UUID(tid) if tid else None,
        agent_id=agent_id,
        limit=limit,
    )
    return [AuditLogOut.model_validate(log) for log in logs]


# ─── Dashboard Stats ────────────────────────────────────


@router.get("/stats")
async def get_enterprise_stats(tenant_id: str | None = None, current_user: UserRecord = Depends(get_current_admin)):
    """Get enterprise dashboard statistics, optionally scoped to a tenant."""
    if tenant_id and current_user.role != "platform_admin" and str(current_user.tenant_id) != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot access other tenant's stats")

    if current_user.role != "platform_admin":
        if current_user.tenant_id is None:
            raise HTTPException(status_code=403, detail="Organization admin must belong to a company")
        tid: uuid.UUID | None = current_user.tenant_id
    elif tenant_id:
        tid = uuid.UUID(tenant_id) if isinstance(tenant_id, str) else tenant_id
    else:
        tid = current_user.tenant_id

    if tid:
        total_agents = await agent_dao.count_for_tenant(tid)
        running_agents = await agent_dao.count_for_tenant(tid, status="running")
        total_users = await user_dao.count_active(tenant_id=tid)
        pending_approvals = await approval_request_dao.count_pending(tenant_id=tid)
    else:
        # Platform-wide: thrifty counts via existing helpers where available
        from app.db.session import connection_ctx

        async with connection_ctx() as conn:
            total_agents = int_from_row(await conn.fetchval("SELECT COUNT(*) FROM agents"))
            running_agents = int_from_row(await conn.fetchval("SELECT COUNT(*) FROM agents WHERE status = 'running'"))
        total_users = await user_dao.count_active()
        pending_approvals = await approval_request_dao.count_pending()

    return {
        "total_agents": total_agents,
        "running_agents": running_agents,
        "total_users": total_users,
        "pending_approvals": pending_approvals,
    }


# ─── Tenant Quota Settings ──────────────────────────────


class TenantQuotaUpdate(BaseModel):
    default_message_limit: int | None = None
    default_message_period: str | None = None
    default_max_agents: int | None = None
    default_agent_ttl_hours: int | None = None
    default_max_llm_calls_per_day: int | None = None
    min_heartbeat_interval_minutes: int | None = None
    default_max_triggers: int | None = None
    min_poll_interval_floor: int | None = None
    max_webhook_rate_ceiling: int | None = None


@router.get("/tenant-quotas")
async def get_tenant_quotas(current_user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    """Get tenant quota defaults and heartbeat settings."""
    if not current_user.tenant_id:
        return {}
    tenant = await tenant_dao.get(current_user.tenant_id)
    if not tenant:
        return {}
    return {
        "default_message_limit": tenant.default_message_limit,
        "default_message_period": tenant.default_message_period,
        "default_max_agents": tenant.default_max_agents,
        "default_agent_ttl_hours": tenant.default_agent_ttl_hours,
        "default_max_llm_calls_per_day": tenant.default_max_llm_calls_per_day,
        "min_heartbeat_interval_minutes": tenant.min_heartbeat_interval_minutes,
        "default_max_triggers": tenant.default_max_triggers,
        "min_poll_interval_floor": tenant.min_poll_interval_floor,
        "max_webhook_rate_ceiling": tenant.max_webhook_rate_ceiling,
    }


@router.patch("/tenant-quotas")
async def update_tenant_quotas(
    data: TenantQuotaUpdate, current_user: UserRecord = Depends(get_current_admin)
) -> dict[str, Any]:
    """Update tenant quota defaults (admin only). Enforces heartbeat floor on existing agents."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")

    tenant = await tenant_dao.get(current_user.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    updates = object_mapping_from(data.model_dump(exclude_unset=True))
    adjusted_count = 0
    floor = updates.get("min_heartbeat_interval_minutes")
    if updates:
        tenant = await tenant_dao.update(db_obj=tenant, obj_in=updates)

    if floor is not None:
        from app.services.quota_guard import enforce_heartbeat_floor

        adjusted_count = await enforce_heartbeat_floor(tenant.id, floor=json_as_int(floor), db=None)

    return {
        "message": "Tenant quotas updated",
        "heartbeat_agents_adjusted": adjusted_count,
    }


# ── System Email: Test & Templates ──────────────────────


class TestEmailRequest(BaseModel):
    email: str


@router.post("/system-email/test")
async def send_test_email_endpoint(
    data: TestEmailRequest, current_user: UserRecord = Depends(get_current_admin)
) -> dict[str, Any]:
    """Send a test email to verify SMTP configuration (admin only)."""
    import smtplib
    import ssl

    from app.services.system_email_service import send_test_email

    _ = current_user
    try:
        await send_test_email(data.email)
        return {"success": True, "message": f"Test email sent to {data.email}"}
    except smtplib.SMTPAuthenticationError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                "SMTP authentication failed. Please check that the SMTP username is the full email address "
                + "and that the password/app password is valid for this mailbox."
            ),
        ) from e
    except (TimeoutError, ssl.SSLError) as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"SMTP TLS/connect timed out: {e}. Please verify the SMTP host, port, and SSL/TLS mode. "
                + "For Zoho, the SMTP host depends on the account data center, for example smtp.zoho.com "
                + "or smtp.zoho.com.cn."
            ),
        ) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/email-templates")
async def get_email_templates_endpoint(current_user: UserRecord = Depends(get_current_admin)) -> dict[str, Any]:
    """Get email templates (current values + available variables per scenario)."""
    from app.services.system_email_service import (
        DEFAULT_EMAIL_TEMPLATES,
        EMAIL_TEMPLATE_VARIABLES,
        get_email_templates,
    )

    _ = current_user
    templates = await get_email_templates()
    return {
        "templates": templates,
        "variables": EMAIL_TEMPLATE_VARIABLES,
        "defaults": DEFAULT_EMAIL_TEMPLATES,
    }


class EmailTemplatesUpdate(BaseModel):
    templates: JsonObject


@router.put("/email-templates")
async def update_email_templates_endpoint(
    data: EmailTemplatesUpdate, current_user: UserRecord = Depends(get_current_admin)
) -> dict[str, Any]:
    """Save email templates (admin only)."""
    from app.services.system_email_service import EMAIL_TEMPLATE_VARIABLES

    _ = current_user
    for key in data.templates:
        if key not in EMAIL_TEMPLATE_VARIABLES:
            raise HTTPException(status_code=400, detail=f"Unknown email template scenario: {key}")

    setting = await system_setting_dao.get_by_key("email_templates")
    if setting:
        _ = await system_setting_dao.update(db_obj=setting, obj_in={"value": data.templates})
    else:
        _ = await system_setting_dao.create(obj_in={"key": "email_templates", "value": data.templates})
    return {"success": True, "message": "Email templates saved"}


# ─── System Settings ───────────────────────────────────


class SettingUpdate(BaseModel):
    value: JsonObject


@router.get("/system-settings/notification_bar/public")
async def get_notification_bar_public() -> dict[str, Any]:
    """Public (no auth) endpoint to read the notification bar config."""
    setting = await system_setting_dao.get_by_key("notification_bar")
    if not setting or not setting.value:
        return {"enabled": False, "text": "", "updated_at": None}
    return {
        "enabled": setting.value.get("enabled", False),
        "text": setting.value.get("text", ""),
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


@router.get("/system-settings/{key}")
async def get_system_setting(key: str, current_user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    """Get a system setting by key."""
    _ = current_user
    setting = await system_setting_dao.get_by_key(key)
    if not setting:
        return {"key": key, "value": {}}
    return {
        "key": setting.key,
        "value": setting.value,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


@router.put("/system-settings/{key}")
async def update_system_setting(
    key: str, data: SettingUpdate, current_user: UserRecord = Depends(get_current_admin)
) -> dict[str, Any]:
    """Create or update a system setting."""
    if key == "platform" and not _is_platform_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only platform admin can modify platform settings")
    setting = await system_setting_dao.get_by_key(key)
    if setting:
        setting = await system_setting_dao.update(db_obj=setting, obj_in={"value": data.value})
    else:
        setting = await system_setting_dao.create(obj_in={"key": key, "value": data.value})

    if key == "platform" and data.value.get("public_base_url"):
        await _regenerate_all_sso_domains()

    return {
        "key": setting.key,
        "value": setting.value,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


# ─── SSO Derived State Helper ───────────────────────────


async def _sync_tenant_sso_state(tenant_id: uuid.UUID) -> None:
    """Recompute tenant.sso_enabled based on channel-level sso_login_enabled flags."""
    active_sso_count = await identity_provider_dao.count_active_sso(tenant_id)
    tenant = await tenant_dao.get(tenant_id)
    if not tenant:
        return

    updates: dict[str, Any] = {"sso_enabled": active_sso_count > 0}

    if active_sso_count > 0 and not tenant.sso_domain:
        sso_base = await platform_service.get_tenant_sso_base_url(tenant)
        host = sso_base.split("://")[-1].split(":")[0].split("/")[0]
        is_ip = platform_service.is_ip_address(host)

        if is_ip:
            await tenant_dao.clear_sso_domain_except(tenant_id)
            logger.info(f"[SSO] IP mode: cleared sso_domain for all other tenants, setting for tenant_id={tenant_id}")

        updates["sso_domain"] = sso_base

    _ = await tenant_dao.update(db_obj=tenant, obj_in=updates)


async def _regenerate_all_sso_domains() -> None:
    """Regenerate sso_domain for ALL tenants when public_base_url changes."""
    base_url = await platform_service.get_public_base_url(None)
    host = base_url.split("://")[-1].split(":")[0].split("/")[0]
    is_ip = platform_service.is_ip_address(host)

    tenants = await tenant_dao.list_for_sso_regen()

    for i, tenant in enumerate(tenants):
        if is_ip:
            if i == 0:
                sso_base = await platform_service.get_tenant_sso_base_url(tenant)
                _ = await tenant_dao.update(db_obj=tenant, obj_in={"sso_domain": sso_base})
            else:
                _ = await tenant_dao.update(db_obj=tenant, obj_in={"sso_domain": None})
        else:
            sso_base = await platform_service.get_tenant_sso_base_url(tenant)
            _ = await tenant_dao.update(db_obj=tenant, obj_in={"sso_domain": sso_base})
        logger.info(f"[SSO regen] tenant={tenant.slug} sso_domain={tenant.sso_domain if i == 0 or not is_ip else None}")


# ─── Identity Providers ─────────────────────────────────


@router.get("/identity-providers", response_model=list[IdentityProviderOut])
async def list_identity_providers(
    tenant_id: str | None = None, global_only: bool = False, current_user: UserRecord = Depends(get_current_user)
):
    """List identity providers configured for the tenant."""
    if tenant_id and not _is_platform_admin_user(current_user) and str(current_user.tenant_id) != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot access other tenant's providers")

    if global_only:
        if not _is_platform_admin_user(current_user):
            raise HTTPException(status_code=403, detail="Only platform admin can access global identity providers")
        providers = await identity_provider_dao.list_global()
    else:
        tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
        if not tid and not _is_platform_admin_user(current_user):
            raise HTTPException(status_code=400, detail="tenant_id is required for identity providers")
        if tid:
            providers = await identity_provider_dao.list_for_tenant(uuid.UUID(tid))
        else:
            providers = await identity_provider_dao.list_global()

    return [_identity_provider_response(provider) for provider in providers]


class IdentityProviderCreate(BaseModel):
    provider_type: str
    name: str
    is_active: bool = True
    sso_login_enabled: bool = False
    config: JsonObject = Field(default_factory=dict)
    tenant_id: uuid.UUID | None = None


class OAuth2Config(BaseModel):
    """OAuth2 provider configuration with friendly field names."""

    app_id: str | None = None
    app_secret: str | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    user_info_url: str | None = None
    scope: str | None = "openid profile email"

    def to_config_dict(self) -> JsonObject:
        config: JsonObject = {}
        if self.app_id:
            config["app_id"] = self.app_id
            config["client_id"] = self.app_id
        if self.app_secret:
            config["app_secret"] = self.app_secret
            config["client_secret"] = self.app_secret
        if self.authorize_url:
            config["authorize_url"] = self.authorize_url
        if self.token_url:
            config["token_url"] = self.token_url
        if self.user_info_url:
            config["user_info_url"] = self.user_info_url
        if self.scope:
            config["scope"] = self.scope
        return config

    @classmethod
    def from_config_dict(cls, config: JsonObject) -> OAuth2Config:
        app_id = config.get("app_id")
        client_id = config.get("client_id")
        app_secret = config.get("app_secret")
        client_secret = config.get("client_secret")
        authorize_url = config.get("authorize_url")
        token_url = config.get("token_url")
        user_info_url = config.get("user_info_url")
        scope = config.get("scope")
        return cls(
            app_id=app_id if isinstance(app_id, str) else client_id if isinstance(client_id, str) else None,
            app_secret=app_secret
            if isinstance(app_secret, str)
            else client_secret
            if isinstance(client_secret, str)
            else None,
            authorize_url=authorize_url if isinstance(authorize_url, str) else None,
            token_url=token_url if isinstance(token_url, str) else None,
            user_info_url=user_info_url if isinstance(user_info_url, str) else None,
            scope=scope if isinstance(scope, str) else None,
        )


class IdentityProviderOAuth2Create(BaseModel):
    """Simplified OAuth2 provider creation with dedicated fields."""

    provider_type: str = "oauth2"
    name: str
    is_active: bool = True
    app_id: str
    app_secret: str
    authorize_url: str
    token_url: str
    user_info_url: str
    scope: str | None = "openid profile email"
    tenant_id: uuid.UUID | None = None


def normalize_oauth2_config(config: JsonObject) -> JsonObject:
    """Normalize OAuth2 config to use both naming conventions for compatibility."""
    if "app_id" in config or "app_secret" in config or "authorize_url" in config:
        normalized: JsonObject = {}
        if "app_id" in config:
            normalized["app_id"] = config["app_id"]
            normalized["client_id"] = config["app_id"]
        elif "client_id" in config:
            normalized["app_id"] = config["client_id"]
            normalized["client_id"] = config["client_id"]

        if "app_secret" in config:
            normalized["app_secret"] = config["app_secret"]
            normalized["client_secret"] = config["app_secret"]
        elif "client_secret" in config:
            normalized["app_secret"] = config["client_secret"]
            normalized["client_secret"] = config["client_secret"]

        for key in ["authorize_url", "token_url", "user_info_url", "scope"]:
            if key in config:
                normalized[key] = config[key]

        return normalized
    return config


def validate_provider_config(provider_type: str, config: JsonObject) -> None:
    """Validate identity provider config. Specific field checks are handled by the frontend."""
    if provider_type in {"google", "github"}:
        client_id = config.get("client_id") or config.get("app_id")
        client_secret = config.get("client_secret") or config.get("app_secret")
        if not isinstance(client_id, str) or not isinstance(client_secret, str) or not client_id or not client_secret:
            raise HTTPException(status_code=422, detail=f"{provider_type} requires client_id and client_secret")


def _sanitize_identity_provider_config(provider_type: str, config: JsonObject | None) -> JsonObject | None:
    if config is None:
        return None
    sanitized = dict(config)
    if provider_type == "google_workspace":
        _ = sanitized.pop("google_admin_refresh_token", None)
        _ = sanitized.pop("google_admin_refresh_token_encrypted", None)
    return sanitized


def _identity_provider_response(
    provider: IdentityProviderRecord | Any, sso_domain: str | None = None
) -> dict[str, object]:
    data = IdentityProviderOut.model_validate(provider).model_dump()
    data["config"] = _sanitize_identity_provider_config(provider.provider_type, provider.config)
    data["last_synced_at"] = (provider.config or {}).get("last_synced_at")
    if sso_domain is not None:
        data["sso_domain"] = sso_domain
    return data


@router.post("/identity-providers", response_model=IdentityProviderOut)
async def create_identity_provider(data: IdentityProviderCreate, current_user: UserRecord = Depends(get_current_admin)):
    """Create a new identity provider (Admin only)."""
    from app.services.auth_registry import auth_provider_registry

    validate_provider_config(data.provider_type, data.config)

    tid = data.tenant_id
    is_platform_admin = _is_platform_admin_user(current_user)
    if not is_platform_admin:
        if tid is None:
            tid = current_user.tenant_id
        elif str(tid) != str(current_user.tenant_id):
            raise HTTPException(status_code=403, detail="Can only create providers for your own tenant")

    if not tid and not (is_platform_admin and data.provider_type in {"google", "github"}):
        raise HTTPException(status_code=400, detail="tenant_id is required to create an identity provider")

    if data.sso_login_enabled and tid is not None and not await sso_service.validate_sso_enablement(tid):
        raise HTTPException(
            status_code=400,
            detail="IP address does not support multi-tenant SSO. Another tenant already has SSO enabled.",
        )

    provider = await identity_provider_dao.create(
        obj_in={
            "provider_type": data.provider_type,
            "name": data.name,
            "is_active": data.is_active,
            "sso_login_enabled": data.sso_login_enabled,
            "config": data.config,
            "tenant_id": tid,
        }
    )
    auth_provider_registry._clear_cache(provider.provider_type)
    return _identity_provider_response(provider)


@router.post("/identity-providers/oauth2", response_model=IdentityProviderOut)
async def create_oauth2_provider(
    data: IdentityProviderOAuth2Create, current_user: UserRecord = Depends(get_current_admin)
):
    """Create a new OAuth2 identity provider with simplified fields."""
    from app.services.auth_registry import auth_provider_registry

    oauth_config = OAuth2Config(
        app_id=data.app_id,
        app_secret=data.app_secret,
        authorize_url=data.authorize_url,
        token_url=data.token_url,
        user_info_url=data.user_info_url,
        scope=data.scope,
    )
    config = oauth_config.to_config_dict()
    validate_provider_config("oauth2", config)

    tid = data.tenant_id
    if not _is_platform_admin_user(current_user):
        if tid is None:
            tid = current_user.tenant_id
        elif str(tid) != str(current_user.tenant_id):
            raise HTTPException(status_code=403, detail="Can only create providers for your own tenant")

    if not tid:
        raise HTTPException(status_code=400, detail="tenant_id is required to create an identity provider")

    provider = await identity_provider_dao.create(
        obj_in={
            "provider_type": "oauth2",
            "name": data.name,
            "is_active": data.is_active,
            "config": config,
            "tenant_id": tid,
        }
    )
    auth_provider_registry._clear_cache(provider.provider_type)
    return _identity_provider_response(provider)


class OAuth2ConfigUpdate(BaseModel):
    """OAuth2 provider configuration update with dedicated fields."""

    name: str | None = None
    is_active: bool | None = None
    app_id: str | None = None
    app_secret: str | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    user_info_url: str | None = None
    scope: str | None = None


@router.patch("/identity-providers/{provider_id}/oauth2", response_model=IdentityProviderOut)
async def update_oauth2_provider(
    provider_id: uuid.UUID, data: OAuth2ConfigUpdate, current_user: UserRecord = Depends(get_current_admin)
):
    """Update an OAuth2 identity provider with simplified fields."""
    from app.services.auth_registry import auth_provider_registry

    provider = await identity_provider_dao.get(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if provider.provider_type != "oauth2":
        raise HTTPException(status_code=400, detail="Provider is not an OAuth2 provider")

    if not _is_platform_admin_user(current_user) and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this provider")

    updates: dict[str, Any] = {}
    if data.name is not None:
        updates["name"] = data.name
    if data.is_active is not None:
        updates["is_active"] = data.is_active

    if any(
        [data.app_id, data.app_secret is not None, data.authorize_url, data.token_url, data.user_info_url, data.scope]
    ):
        current_config = dict(provider.config or {})

        if data.app_id is not None:
            current_config["app_id"] = data.app_id
            current_config["client_id"] = data.app_id
        if data.app_secret is not None:
            if data.app_secret:
                current_config["app_secret"] = data.app_secret
                current_config["client_secret"] = data.app_secret
            else:
                current_config.pop("app_secret", None)
                current_config.pop("client_secret", None)
        if data.authorize_url is not None:
            current_config["authorize_url"] = data.authorize_url
        if data.token_url is not None:
            current_config["token_url"] = data.token_url
        if data.user_info_url is not None:
            current_config["user_info_url"] = data.user_info_url
        if data.scope is not None:
            current_config["scope"] = data.scope

        validate_provider_config("oauth2", current_config)
        updates["config"] = current_config

    provider = await identity_provider_dao.update(db_obj=provider, obj_in=updates)
    auth_provider_registry._clear_cache(provider.provider_type)
    return _identity_provider_response(provider)


class IdentityProviderUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    sso_login_enabled: bool | None = None
    config: JsonObject | None = None


@router.put("/identity-providers/{provider_id}", response_model=IdentityProviderOut)
async def update_identity_provider(
    provider_id: uuid.UUID, data: IdentityProviderUpdate, current_user: UserRecord = Depends(get_current_admin)
):
    """Update an existing identity provider."""
    from app.services.auth_registry import auth_provider_registry

    provider = await identity_provider_dao.get(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if not _is_platform_admin_user(current_user) and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this provider")

    updates: dict[str, Any] = {}
    if data.name is not None:
        updates["name"] = data.name
    if data.is_active is not None:
        updates["is_active"] = data.is_active
    if data.sso_login_enabled is not None:
        if data.sso_login_enabled is True and not provider.sso_login_enabled:
            if provider.tenant_id is None:
                raise HTTPException(status_code=400, detail="tenant_id is required to enable SSO login")
            if not await sso_service.validate_sso_enablement(provider.tenant_id):
                raise HTTPException(
                    status_code=400,
                    detail="IP address does not support multi-tenant SSO. Another tenant already has SSO enabled.",
                )
        updates["sso_login_enabled"] = data.sso_login_enabled
    if data.config is not None:
        new_config = dict(provider.config or {})
        new_config.update(data.config)
        validate_provider_config(provider.provider_type, new_config)
        updates["config"] = new_config

    provider = await identity_provider_dao.update(db_obj=provider, obj_in=updates)
    auth_provider_registry._clear_cache(provider.provider_type)

    sso_domain = None
    if data.sso_login_enabled is not None and provider.tenant_id:
        await _sync_tenant_sso_state(provider.tenant_id)
        t = await tenant_dao.get(provider.tenant_id)
        if t:
            sso_domain = t.sso_domain

    return _identity_provider_response(provider, sso_domain=sso_domain)


@router.delete("/identity-providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity_provider(provider_id: uuid.UUID, current_user: UserRecord = Depends(get_current_admin)):
    """Delete an identity provider."""
    provider = await identity_provider_dao.get(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if not _is_platform_admin_user(current_user) and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this provider")

    try:
        await identity_provider_dao.delete_nullifying_org_refs(provider_id)
    except Exception as e:
        logger.error(f"Failed to delete identity provider {provider_id}: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to delete identity provider due to database constraints"
        ) from e


# ─── Org Structure ──────────────────────────────────────


@router.get("/org/departments")
async def list_org_departments(
    tenant_id: str | None = None, provider_id: str | None = None, current_user: UserRecord = Depends(get_current_user)
) -> dict[str, Any]:
    """List all departments, optionally filtered by tenant or provider."""
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    is_global_admin = current_user.role == "platform_admin" and not effective_tenant_id

    if tenant_id:
        if not is_global_admin and effective_tenant_id and effective_tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Cannot access other tenant's data")
    else:
        tenant_id = effective_tenant_id

    rows = await org_department_dao.list_active_with_provider(
        tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
        provider_id=uuid.UUID(provider_id) if provider_id else None,
    )
    total_member = await org_member_dao.count_active(
        tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
        provider_id=uuid.UUID(provider_id) if provider_id else None,
    )

    items: list[dict[str, object]] = []
    for row in rows:
        mapping = object_mapping_from(row)
        provider_id_val = uuid_from_row_opt(mapping.get("provider_id"))
        parent_id_val = uuid_from_row_opt(mapping.get("parent_id"))
        items.append(
            {
                "id": str(uuid_from_row(mapping["id"])),
                "external_id": mapping.get("external_id"),
                "provider_id": str(provider_id_val) if provider_id_val else None,
                "provider_name": mapping.get("provider_name") if provider_id_val else None,
                "provider_type": mapping.get("provider_type") if provider_id_val else None,
                "name": mapping.get("name"),
                "parent_id": str(parent_id_val) if parent_id_val else None,
                "path": mapping.get("path"),
                "member_count": mapping.get("member_count"),
            }
        )
    return {
        "items": items,
        "total_member": total_member,
    }


@router.get("/org/members")
async def list_org_members(
    department_id: str | None = None,
    search: str | None = None,
    tenant_id: str | None = None,
    provider_id: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List org members, optionally filtered by department, search, tenant, or provider."""
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    is_global_admin = current_user.role == "platform_admin" and not effective_tenant_id

    if tenant_id:
        if not is_global_admin and effective_tenant_id and effective_tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Cannot access other tenant's data")
    else:
        tenant_id = effective_tenant_id

    dept_ids = None
    if department_id:
        dept_ids = await org_department_dao.subtree_ids(uuid.UUID(department_id))

    pairs = await org_member_dao.list_active_filtered(
        tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
        provider_id=uuid.UUID(provider_id) if provider_id else None,
        department_ids=dept_ids,
        search=search,
        limit=100,
    )
    members = [m for m, _pn, _pt in pairs]
    member_paths = await derive_member_department_paths(None, members)
    return [
        {
            "id": str(m.id),
            "name": m.name,
            "email": m.email,
            "title": m.title,
            "department_path": member_paths.get(m.id, m.department_path),
            "avatar_url": m.avatar_url,
            "external_id": m.external_id,
            "provider_id": str(m.provider_id) if m.provider_id else None,
            "provider_name": provider_name if m.provider_id else None,
            "provider_type": provider_type if m.provider_id else None,
        }
        for m, provider_name, provider_type in pairs
    ]


@router.post("/org/sync")
async def trigger_org_sync(provider_id: str | None = None, current_user: UserRecord = Depends(get_current_admin)):
    """Manually trigger org structure sync from a specific identity provider."""
    from app.services.org_sync_service import org_sync_service

    if not provider_id:
        raise HTTPException(status_code=400, detail="provider_id is required")

    try:
        pid = uuid.UUID(provider_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid provider_id") from e

    provider = await identity_provider_dao.get(pid)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if not provider.tenant_id:
        raise HTTPException(status_code=400, detail="Provider must be bound to a tenant")

    if not _is_platform_admin_user(current_user) and provider.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot sync other tenant's provider")

    return await org_sync_service.sync_provider(None, provider_id)


@router.get("/org/wecom-verify/{provider_id}")
async def wecom_org_sync_verify(
    provider_id: uuid.UUID, msg_signature: str = "", timestamp: str = "", nonce: str = "", echostr: str = ""
):
    """Handle WeCom receive-message-server URL verification for the org sync app."""
    from fastapi.responses import Response as _Response

    from app.api.wecom import _decrypt_msg, _verify_signature

    provider = await identity_provider_dao.get(provider_id)
    if not provider:
        return _Response(status_code=404)

    config = json_object_from(provider.config)
    token = json_as_str_or(config.get("verify_token"))
    aes_key = json_as_str_or(config.get("verify_aes_key"))

    if not isinstance(token, str) or not isinstance(aes_key, str) or not token or not aes_key:
        logger.warning(
            f"[WeCom Verify] Provider {provider_id} is missing verify_token or verify_aes_key in config. "
            + "Please configure them in the WeCom provider settings."
        )
        return _Response(status_code=400)

    expected_sig = _verify_signature(token, timestamp, nonce, echostr)
    if expected_sig != msg_signature:
        logger.warning(f"[WeCom Verify] Signature mismatch for provider {provider_id}")
        return _Response(status_code=403)

    try:
        decrypted, _ = _decrypt_msg(aes_key, echostr)
        logger.info(f"[WeCom Verify] Successfully verified org sync callback for provider {provider_id}")
        return _Response(content=decrypted, media_type="text/plain")
    except Exception as e:
        logger.error(f"[WeCom Verify] Failed to decrypt echostr for provider {provider_id}: {e}")
        return _Response(status_code=500)


@router.get("/org/wecom-callback/{token}", include_in_schema=False)
async def wecom_callback_verify_universal(
    token: str,
    aes_key: str = "",
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
):
    """Universal WeCom callback URL verification endpoint (no database lookup required)."""
    from fastapi.responses import Response as _Response

    from app.api.wecom import _decrypt_msg, _verify_signature

    if not token:
        return _Response(status_code=400, content="verify_token is required in URL path")

    if not aes_key:
        logger.warning("[WeCom Callback] Missing aes_key query param in universal callback URL")
        return _Response(status_code=400, content="aes_key query param is required")

    expected_sig = _verify_signature(token, timestamp, nonce, echostr)
    if expected_sig != msg_signature:
        logger.warning(
            f"[WeCom Callback] Signature mismatch: token={token[:8]}... "
            + f"expected={expected_sig[:16]}... got={msg_signature[:16]}..."
        )
        return _Response(status_code=403)

    try:
        decrypted, _ = _decrypt_msg(aes_key, echostr)
        logger.info(f"[WeCom Callback] Universal callback verified successfully for token={token[:8]}...")
        return _Response(content=decrypted, media_type="text/plain")
    except Exception as e:
        logger.error(f"[WeCom Callback] Failed to decrypt echostr: {e}")
        return _Response(status_code=500)


# ─── Invitation Codes ───────────────────────────────────


class InvitationCodeCreate(BaseModel):
    count: int = 1
    max_uses: int = 1


def _require_tenant_admin(current_user: UserRecord) -> uuid.UUID:
    """Check that the user is org_admin or platform_admin with a tenant."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Requires admin privileges")
    if current_user.tenant_id is None:
        raise HTTPException(status_code=400, detail="No company assigned")
    return current_user.tenant_id


async def _ensure_invitation_email_enabled(db: object | None = None) -> None:
    """Require enabled system email before accepting email invitations."""
    from app.services.system_email_service import resolve_email_config_async

    if await resolve_email_config_async(db):
        return
    if await resolve_email_config_async(db, include_disabled=True):
        raise HTTPException(
            status_code=400,
            detail="System email SMTP is configured but disabled. Enable system email before sending invitations.",
        )
    raise HTTPException(
        status_code=400,
        detail="System email SMTP settings are not configured. Configure system email before sending invitations.",
    )


def _new_invitation_code() -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


@router.post("/invitation-codes")
async def create_invitation_codes(
    data: InvitationCodeCreate, current_user: UserRecord = Depends(get_current_user)
) -> dict[str, Any]:
    """Batch-create invitation codes for the current user's company."""
    tenant_id = _require_tenant_admin(current_user)
    codes_created = []
    for _ in range(min(data.count, 100)):
        code_str = _new_invitation_code()
        _ = await invitation_code_dao.create(
            obj_in={
                "code": code_str,
                "tenant_id": tenant_id,
                "max_uses": data.max_uses,
                "created_by": current_user.id,
            }
        )
        codes_created.append(code_str)

    return {"created": len(codes_created), "codes": codes_created}


@router.post("/invite-users")
async def invite_users(
    request: Request,
    data: UserInviteRequest,
    background_tasks: BackgroundTasks,
    current_user: UserRecord = Depends(get_current_user),
) -> dict[str, Any]:
    """Batch-invite users via email to the current user's company."""
    tenant_id = _require_tenant_admin(current_user)
    if not data.emails:
        raise HTTPException(status_code=400, detail="No emails provided")

    from app.services.system_email_service import send_company_invitation_email

    tenant = await tenant_dao.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")

    await _ensure_invitation_email_enabled(None)

    base_url = await platform_service.get_public_base_url(None, request=request)

    invited_count = 0
    for email in data.emails:
        email = email.lower().strip()
        if not email:
            continue

        code_str = _new_invitation_code()
        _ = await invitation_code_dao.create(
            obj_in={
                "code": code_str,
                "tenant_id": tenant_id,
                "max_uses": 1,
                "created_by": current_user.id,
            }
        )

        invite_url = f"{base_url}/login?code={code_str}&email={email}"
        inviter_name = current_user.display_name or current_user.username or "Admin"

        background_tasks.add_task(
            send_company_invitation_email,
            to=email,
            inviter_name=inviter_name,
            company_name=tenant.name,
            invite_url=invite_url,
        )
        invited_count += 1

    return {"invited": invited_count, "message": "Invitations sent successfully"}


@router.get("/invitation-codes")
async def list_invitation_codes(
    page: int = 1, page_size: int = 20, search: str = "", current_user: UserRecord = Depends(get_current_user)
) -> dict[str, Any]:
    """List invitation codes for the current user's company."""
    tenant_id = _require_tenant_admin(current_user)

    search_term = search or None
    total = await invitation_code_dao.count_for_tenant(tenant_id, search=search_term)
    offset = (max(page, 1) - 1) * page_size
    codes = await invitation_code_dao.list_for_tenant(
        tenant_id,
        search=search_term,
        offset=offset,
        limit=page_size,
    )
    return {
        "items": [
            {
                "id": str(c.id),
                "code": c.code,
                "max_uses": c.max_uses,
                "used_count": c.used_count,
                "is_active": c.is_active,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in codes
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/invitation-codes/export")
async def export_invitation_codes_csv(current_user: UserRecord = Depends(get_current_user)):
    """Export invitation codes for the current user's company as CSV."""
    tenant_id = _require_tenant_admin(current_user)
    import csv
    import io

    from fastapi.responses import StreamingResponse

    codes = await invitation_code_dao.list_all_for_tenant(tenant_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Code", "Max Uses", "Used Count", "Active", "Created At"])
    for c in codes:
        writer.writerow(
            [
                c.code,
                c.max_uses,
                c.used_count,
                "Yes" if c.is_active else "No",
                c.created_at.strftime("%Y-%m-%d %H:%M:%S") if c.created_at else "",
            ]
        )

    _ = output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invitation_codes.csv"},
    )


@router.delete("/invitation-codes/{code_id}")
async def deactivate_invitation_code(code_id: str, current_user: UserRecord = Depends(get_current_user)):
    """Deactivate an invitation code (must belong to current user's company)."""
    tenant_id = _require_tenant_admin(current_user)

    code = await invitation_code_dao.get_for_tenant(uuid.UUID(code_id), tenant_id)
    if not code:
        raise HTTPException(status_code=404, detail="Code not found")
    _ = await invitation_code_dao.update(db_obj=code, obj_in={"is_active": False})
    return {"status": "deactivated"}
