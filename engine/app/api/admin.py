"""Platform Admin company management API.

Provides endpoints for platform admins to manage companies, view stats,
and control platform-level settings.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.core.security import require_role
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.admin_audit_dao import admin_audit_log_dao
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_session_dao
from app.dao.system_setting_dao import system_setting_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.tool_dao import tool_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord
from app.services.admin_audit import field_change, write_admin_audit
from app.services.admin_provisioning import (
    AdminActivationError,
    create_additional_platform_admin,
    is_genesis_platform_admin,
    set_peer_admin_active,
)
from app.services.tenant_provisioning import AdminEmailTakenError, create_tenant_with_org_admin


async def get_client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


router = APIRouter(prefix="/admin", tags=["admin"])


# ─── Schemas ────────────────────────────────────────────


class CompanyStats(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    sso_enabled: bool = False
    sso_domain: str | None = None
    created_at: datetime | None = None
    user_count: int = 0
    agent_count: int = 0
    agent_running_count: int = 0
    total_tokens: int = 0
    cache_read_tokens_total: int = 0
    org_admin_email: str | None = None


class CompanyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    admin_email: EmailStr
    admin_password: str = Field(min_length=6, max_length=128)
    admin_display_name: str | None = Field(default=None, max_length=200)


class CompanyCreateResponse(BaseModel):
    company: CompanyStats
    org_admin_email: str
    must_change_password: bool = True


class PlatformAdminCreateRequest(BaseModel):
    admin_email: EmailStr
    admin_password: str = Field(min_length=6, max_length=128)
    admin_display_name: str | None = Field(default=None, max_length=200)


class PlatformAdminCreateResponse(BaseModel):
    user_id: uuid.UUID
    admin_email: str
    must_change_password: bool = True


class AdminActiveUpdate(BaseModel):
    is_active: bool


class PlatformAdminOut(BaseModel):
    id: uuid.UUID
    email: str | None = None
    display_name: str | None = None
    role: str
    is_active: bool
    is_genesis: bool = False
    created_at: datetime | None = None


class AdminAuditLogOut(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None = None
    actor_role: str
    actor_email: str | None = None
    action: str
    target_type: str
    target_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    changes: dict[str, Any]
    details: dict[str, Any]
    ip_address: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class PlatformSettingsOut(BaseModel):
    allow_self_create_company: bool = False
    invitation_code_enabled: bool = False
    sso_custom_domain_redirect_enabled: bool = True


class PlatformSettingsUpdate(BaseModel):
    allow_self_create_company: bool | None = None
    invitation_code_enabled: bool | None = None
    sso_custom_domain_redirect_enabled: bool | None = None


# ─── Company Management ────────────────────────────────


@router.get("/companies", response_model=list[CompanyStats])
async def list_companies(current_user: UserRecord = Depends(require_role("platform_admin"))):
    """List all companies with stats."""
    tenants = await tenant_dao.list_ordered_by_created_at(desc=True)
    result = []

    for tenant in tenants:
        tid = tenant.id
        user_count = await user_dao.count_for_tenant(tid)
        agent_count = await agent_dao.count_for_tenant(tid)
        agent_running = await agent_dao.count_for_tenant(tid, status="running")
        total_tokens, cache_read_tokens_total = await agent_dao.sum_tokens_for_tenant(tid)
        org_admin_email = await user_dao.first_org_admin_email(tid)

        result.append(
            CompanyStats(
                id=tenant.id,
                name=tenant.name,
                slug=tenant.slug,
                is_active=tenant.is_active,
                sso_enabled=tenant.sso_enabled,
                sso_domain=tenant.sso_domain,
                created_at=tenant.created_at,
                user_count=user_count,
                agent_count=agent_count,
                agent_running_count=agent_running,
                total_tokens=total_tokens,
                cache_read_tokens_total=cache_read_tokens_total,
                org_admin_email=org_admin_email,
            )
        )

    return result


@router.post("/companies", response_model=CompanyCreateResponse, status_code=201)
async def create_company(
    data: CompanyCreateRequest,
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Create a company and its genesis org admin (platform admin only).

    The org admin is provisioned with the given email + initial password and
    must change the password after the first successful login.
    """
    try:
        provisioned = await create_tenant_with_org_admin(
            name=data.name,
            admin_email=str(data.admin_email),
            admin_password=data.admin_password,
            admin_display_name=data.admin_display_name,
        )
    except AdminEmailTakenError as exc:
        raise HTTPException(status_code=409, detail="Admin email is already registered") from exc

    tenant = provisioned.tenant
    await write_admin_audit(
        actor=current_user,
        action="tenant_create",
        target_type="tenant",
        target_id=tenant.id,
        tenant_id=tenant.id,
        changes={
            "name": field_change(None, tenant.name),
            "org_admin_email": field_change(None, provisioned.admin_email),
        },
        details={"org_admin_user_id": str(provisioned.org_admin.id)},
        ip_address=client_ip,
    )
    return CompanyCreateResponse(
        company=CompanyStats(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            is_active=tenant.is_active,
            created_at=tenant.created_at,
            user_count=1,
            org_admin_email=provisioned.admin_email,
        ),
        org_admin_email=provisioned.admin_email,
        must_change_password=True,
    )


@router.post("/platform-admins", response_model=PlatformAdminCreateResponse, status_code=201)
async def create_platform_admin(
    data: PlatformAdminCreateRequest,
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Create another platform admin. Only the genesis platform admin may call this."""
    if not await is_genesis_platform_admin(current_user):
        raise HTTPException(status_code=403, detail="Only the genesis platform admin can create platform admins")

    try:
        provisioned = await create_additional_platform_admin(
            admin_email=str(data.admin_email),
            admin_password=data.admin_password,
            admin_display_name=data.admin_display_name,
        )
    except AdminEmailTakenError as exc:
        raise HTTPException(status_code=409, detail="Admin email is already registered") from exc

    await write_admin_audit(
        actor=current_user,
        action="platform_admin_create",
        target_type="user",
        target_id=provisioned.user.id,
        changes={
            "role": field_change(None, "platform_admin"),
            "admin_email": field_change(None, provisioned.admin_email),
        },
        details={"must_change_password": True},
        ip_address=client_ip,
    )
    return PlatformAdminCreateResponse(
        user_id=provisioned.user.id,
        admin_email=provisioned.admin_email,
        must_change_password=True,
    )


@router.get("/platform-admins", response_model=list[PlatformAdminOut])
async def list_platform_admins(current_user: UserRecord = Depends(require_role("platform_admin"))):
    """List platform admins. Any platform admin may read; genesis is marked."""
    _ = current_user
    users = await user_dao.list_by_role("platform_admin")
    return [
        PlatformAdminOut(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role,
            is_active=u.is_active,
            is_genesis=bool(getattr(u, "is_genesis", False)),
            created_at=u.created_at,
        )
        for u in users
    ]


@router.patch("/platform-admins/{user_id}/active", response_model=PlatformAdminOut)
async def set_platform_admin_active(
    user_id: uuid.UUID,
    data: AdminActiveUpdate,
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Activate or deactivate another platform admin. Genesis platform admin only."""
    target = await user_dao.get_with_identity(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        updated = await set_peer_admin_active(
            actor=current_user,
            target=target,
            is_active=data.is_active,
            ip_address=client_ip,
        )
    except AdminActivationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return PlatformAdminOut(
        id=updated.id,
        email=updated.email if updated.identity else target.email,
        display_name=updated.display_name,
        role=updated.role,
        is_active=updated.is_active,
        is_genesis=bool(getattr(updated, "is_genesis", getattr(target, "is_genesis", False))),
        created_at=updated.created_at,
    )


@router.get("/audit-logs", response_model=list[AdminAuditLogOut])
async def list_admin_audit_logs(
    tenant_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: int = 100,
    current_user: UserRecord = Depends(require_role("platform_admin")),
):
    """List admin action logs (who / what / when / field changes)."""
    _ = current_user
    logs = await admin_audit_log_dao.list_recent(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        limit=min(max(limit, 1), 500),
    )
    return [AdminAuditLogOut.model_validate(log) for log in logs]


@router.put("/companies/{company_id}/toggle")
async def toggle_company(
    company_id: uuid.UUID,
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Enable or disable a company."""
    tenant = await tenant_dao.get(company_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")

    new_state = not tenant.is_active
    await tenant_dao.update(db_obj=tenant, obj_in={"is_active": new_state})

    if not new_state:
        await agent_dao.pause_running_for_tenant(company_id)

    await write_admin_audit(
        actor=current_user,
        action="company_toggle",
        target_type="tenant",
        target_id=company_id,
        tenant_id=company_id,
        changes={"is_active": field_change(tenant.is_active, new_state)},
        ip_address=client_ip,
    )
    return {"ok": True, "is_active": new_state}


# ─── Platform Metrics Dashboard ─────────────────────────


@router.get("/metrics/timeseries", response_model=list[dict[str, Any]])
async def get_platform_timeseries(
    start_date: datetime, end_date: datetime, current_user: UserRecord = Depends(require_role("platform_admin"))
):
    """Get daily platform metrics within a date range.

    Returns per-day: companies, users, tokens (existing) +
    sessions, DAU, WAU, MAU (new).
    """
    companies_by_day = await tenant_dao.counts_by_created_day(start_date, end_date)
    users_by_day = await user_dao.counts_by_created_day(start_date, end_date)
    tokens_by_day = await agent_activity_log_dao.tokens_by_day(start_date, end_date)
    cache_by_day = await agent_activity_log_dao.cache_read_by_day(start_date, end_date)
    sessions_by_day = await chat_session_dao.counts_by_created_day(start_date, end_date)
    dau_by_day = await chat_session_dao.dau_by_created_day(start_date, end_date)
    wau_by_day, mau_by_day = await chat_session_dao.wau_mau_by_day(
        range_start=start_date - timedelta(days=30),
        range_end=end_date,
        series_start=start_date.date(),
        series_end=end_date.date(),
    )

    result = []
    current_d = start_date.date()
    end_d = end_date.date()

    total_companies = await tenant_dao.count_created_before(start_date)
    total_users = await user_dao.count_created_before(start_date)
    total_tokens, total_cache_read = await agent_dao.sum_tokens_created_before(start_date)
    total_sessions = await chat_session_dao.count_created_before(start_date)

    while current_d <= end_d:
        nc = companies_by_day.get(current_d, 0)
        nu = users_by_day.get(current_d, 0)
        nt = tokens_by_day.get(current_d, 0)
        ncache = cache_by_day.get(current_d, 0)
        ns = sessions_by_day.get(current_d, 0)

        total_companies += nc
        total_users += nu
        total_tokens += nt
        total_cache_read += ncache
        total_sessions += ns

        cache_hit_rate = 0.0 if not nt else round((ncache or 0) / nt, 4)
        result.append(
            {
                "date": current_d.isoformat(),
                "new_companies": nc,
                "total_companies": total_companies,
                "new_users": nu,
                "total_users": total_users,
                "new_tokens": nt,
                "total_tokens": total_tokens,
                "new_cache_read_tokens": ncache,
                "total_cache_read_tokens": total_cache_read,
                "cache_hit_rate": cache_hit_rate,
                "new_sessions": ns,
                "total_sessions": total_sessions,
                "dau": dau_by_day.get(current_d, 0),
                "wau": wau_by_day.get(current_d, 0),
                "mau": mau_by_day.get(current_d, 0),
            }
        )
        current_d += timedelta(days=1)

    return result


@router.get("/metrics/leaderboards")
async def get_platform_leaderboards(current_user: UserRecord = Depends(require_role("platform_admin"))):
    """Get Top 20 token consuming companies and agents."""
    top_companies_raw = await agent_dao.top_token_companies(limit=20)
    top_companies = []
    for row in top_companies_raw:
        total = row["total"] or 0
        top_companies.append(
            {
                "name": row["name"],
                "tokens": row["total"],
                "cache_read_tokens": row["cache_read"],
                "cache_hit_rate": 0.0 if not total else round((row["cache_read"] or 0) / total, 4),
            }
        )

    top_agents_raw = await agent_dao.top_token_agents(limit=20)
    top_agents = []
    for row in top_agents_raw:
        total = row["tokens"] or 0
        top_agents.append(
            {
                "name": row["name"],
                "company": row["company"],
                "tokens": row["tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "cache_hit_rate": 0.0 if not total else round((row["cache_read_tokens"] or 0) / total, 4),
            }
        )

    return {"top_companies": top_companies, "top_agents": top_agents}


@router.get("/metrics/enhanced")
async def get_enhanced_metrics(current_user: UserRecord = Depends(require_role("platform_admin"))):
    """Enhanced platform metrics: retention, avg tokens/session,
    channel distribution, tool categories, and churn warnings.
    """
    now = datetime.now(UTC)
    thirty_days_ago = now - timedelta(days=30)

    total_tok_30d = await agent_activity_log_dao.sum_tokens_since(thirty_days_ago)
    total_sess_30d = await chat_session_dao.count_created_since(thirty_days_ago) or 1
    avg_tokens_per_session = round(total_tok_30d / max(total_sess_30d, 1))

    last_week_total, retained = await chat_session_dao.retention_7d()
    retention_rate = 0.0 if not last_week_total else round(retained * 100.0 / last_week_total, 1)

    channel_distribution = await chat_session_dao.channel_distribution_since(thirty_days_ago)
    tool_category_top10 = await tool_dao.top_enabled_categories(limit=10)
    churn_warnings = await chat_session_dao.churn_warnings()

    return {
        "avg_tokens_per_session_30d": avg_tokens_per_session,
        "retention_rate_7d": retention_rate,
        "last_week_active_companies": last_week_total,
        "retained_companies": retained,
        "channel_distribution": channel_distribution,
        "tool_category_top10": tool_category_top10,
        "churn_warnings": churn_warnings,
    }


# ─── Platform Settings ─────────────────────────────────


@router.get("/platform-settings", response_model=PlatformSettingsOut)
async def get_platform_settings(current_user: UserRecord = Depends(require_role("platform_admin"))):
    """Get platform-level settings."""
    settings: dict[str, bool] = {}

    for key, default in [
        ("allow_self_create_company", False),
        ("invitation_code_enabled", False),
        ("sso_custom_domain_redirect_enabled", True),
    ]:
        settings[key] = await system_setting_dao.is_flag_enabled(key, default=default)

    return PlatformSettingsOut(**settings)


@router.put("/platform-settings", response_model=PlatformSettingsOut)
async def update_platform_settings(
    data: PlatformSettingsUpdate,
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
):
    """Update platform-level settings."""
    updates = data.model_dump(exclude_unset=True)
    previous: dict[str, bool] = {}
    for key in updates:
        previous[key] = await system_setting_dao.is_flag_enabled(key, default=False)

    for key, value in updates.items():
        await system_setting_dao.set_flag(key, bool(value))

    if updates:
        await write_admin_audit(
            actor=current_user,
            action="platform_settings_update",
            target_type="platform_settings",
            changes={key: field_change(previous.get(key), bool(value)) for key, value in updates.items()},
            ip_address=client_ip,
        )

    return await get_platform_settings(current_user=current_user)
