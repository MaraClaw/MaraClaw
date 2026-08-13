"""Platform Admin company management API.

Provides endpoints for platform admins to manage companies, view stats,
and control platform-level settings.
"""

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.core.security import hash_password_async, require_role
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_session_dao
from app.dao.identity_dao import identity_dao
from app.dao.participant_dao import participant_dao
from app.dao.system_setting_dao import system_setting_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.tool_dao import tool_dao
from app.dao.user_dao import user_dao
from app.db.errors import UniqueViolationError
from app.db.session import connection_ctx
from app.records.user import UserRecord

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
    data: CompanyCreateRequest, current_user: UserRecord = Depends(require_role("platform_admin"))
):
    """Create a company and its genesis org admin (platform admin only).

    The org admin is provisioned with the given email + initial password and
    must change the password after the first successful login.
    """
    admin_email = str(data.admin_email).strip().lower()
    if await identity_dao.get_by_email(admin_email):
        raise HTTPException(status_code=409, detail="Admin email is already registered")

    slug = re.sub(r"[^a-z0-9]+", "-", data.name.lower().strip()).strip("-")[:40]
    if not slug:
        slug = "company"
    slug = f"{slug}-{secrets.token_hex(3)}"

    password_hash = await hash_password_async(data.admin_password)
    local_part = admin_email.split("@", 1)[0][:100] or "org-admin"
    username = local_part
    if await identity_dao.is_username_taken(username):
        username = f"{local_part}_{secrets.token_hex(3)}"[:100]

    display_name = (data.admin_display_name or "").strip() or local_part

    try:
        async with connection_ctx():
            tenant = await tenant_dao.create(obj_in={"name": data.name, "slug": slug, "im_provider": "web_only"})

            identity = await identity_dao.create_identity(
                email=admin_email,
                username=username,
                password_hash=password_hash,
                is_platform_admin=False,
                email_verified=True,
                must_change_password=True,
            )

            org_admin = await user_dao.create(
                obj_in={
                    "identity_id": identity.id,
                    "tenant_id": tenant.id,
                    "display_name": display_name,
                    "role": "org_admin",
                    "registration_source": "platform_admin",
                    "is_active": True,
                    "quota_message_limit": tenant.default_message_limit,
                    "quota_message_period": tenant.default_message_period,
                    "quota_max_agents": tenant.default_max_agents,
                    "quota_agent_ttl_hours": tenant.default_agent_ttl_hours,
                }
            )
            # Identity-backed email/phone properties require the association for org directory bind.
            org_admin.identity = identity
            await participant_dao.create_for_user(
                org_admin.id,
                display_name=org_admin.display_name,
                avatar_url=org_admin.avatar_url,
            )

            from app.services.registration_service import registration_service

            await registration_service.bind_org_member(org_admin)
    except UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Admin email is already registered") from exc

    return CompanyCreateResponse(
        company=CompanyStats(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            is_active=tenant.is_active,
            created_at=tenant.created_at,
            user_count=1,
            org_admin_email=admin_email,
        ),
        org_admin_email=admin_email,
        must_change_password=True,
    )


@router.put("/companies/{company_id}/toggle")
async def toggle_company(company_id: uuid.UUID, current_user: UserRecord = Depends(require_role("platform_admin"))):
    """Enable or disable a company."""
    tenant = await tenant_dao.get(company_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Company not found")

    new_state = not tenant.is_active
    await tenant_dao.update(db_obj=tenant, obj_in={"is_active": new_state})

    if not new_state:
        await agent_dao.pause_running_for_tenant(company_id)

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
    data: PlatformSettingsUpdate, current_user: UserRecord = Depends(require_role("platform_admin"))
):
    """Update platform-level settings."""
    updates = data.model_dump(exclude_unset=True)

    for key, value in updates.items():
        await system_setting_dao.set_flag(key, bool(value))

    return await get_platform_settings(current_user=current_user)
