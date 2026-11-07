"""Platform-admin company lists: batched stats, picker options and signed keyset pages.

Authorization belongs to the caller and must precede cursor decoding.
"""

import uuid
from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from collections.abc import Sequence
from datetime import datetime
from hmac import compare_digest, digest
from typing import ClassVar, Final

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.config import get_settings
from app.dao.tenant_dao import TenantUsageStats, tenant_dao
from app.records.tenant import TenantRecord
from app.schemas.admin_companies import CompanyOption, CompanyPageOut, CompanyStats

MAX_CURSOR_LENGTH: Final = 1024
MAX_SEARCH_LENGTH: Final = 200
DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 200
_NO_STATS = TenantUsageStats()


class _CompanyCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(strict=True, ge=1, le=1)
    created_at: AwareDatetime
    id: uuid.UUID
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


def _cursor_signature(created_at: datetime, row_id: uuid.UUID) -> str:
    payload = f"admin-companies-v1:{created_at.isoformat()}:{row_id}".encode()
    return digest(get_settings().JWT_SECRET_KEY.encode(), payload, "sha256").hex()


def _encode_cursor(tenant: TenantRecord) -> str:
    if tenant.created_at is None:
        raise ValueError("tenants.created_at is NOT NULL")
    boundary = _CompanyCursor(
        version=1,
        created_at=tenant.created_at,
        id=tenant.id,
        signature=_cursor_signature(tenant.created_at, tenant.id),
    )
    return urlsafe_b64encode(boundary.model_dump_json().encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> _CompanyCursor:
    if not value or len(value) > MAX_CURSOR_LENGTH:
        raise HTTPException(status_code=400, detail="Invalid cursor")
    try:
        raw = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = _CompanyCursor.model_validate_json(raw, strict=True)
    except (Base64Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc
    if not compare_digest(cursor.signature, _cursor_signature(cursor.created_at, cursor.id)):
        raise HTTPException(status_code=400, detail="Invalid cursor")
    return cursor


def _search_text(q: str | None) -> str:
    query = (q or "").strip()
    if len(query) > MAX_SEARCH_LENGTH:
        raise HTTPException(status_code=400, detail="Search is too long")
    return query


def _with_stats(tenant: TenantRecord, stats: TenantUsageStats) -> CompanyStats:
    return CompanyStats(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        is_active=tenant.is_active,
        sso_enabled=tenant.sso_enabled,
        sso_domain=tenant.sso_domain,
        is_system=tenant.is_system,
        is_default_end_user_org=tenant.is_default_end_user_org,
        created_at=tenant.created_at,
        user_count=stats.user_count,
        active_user_count=stats.active_user_count,
        inactive_user_count=max(stats.user_count - stats.active_user_count, 0),
        agent_count=stats.agent_count,
        agent_running_count=stats.agent_running_count,
        total_tokens=stats.total_tokens,
        cache_read_tokens_total=stats.cache_read_tokens_total,
        org_admin_email=stats.org_admin_email,
    )


async def _stats_rows(tenants: Sequence[TenantRecord]) -> list[CompanyStats]:
    usage = await tenant_dao.usage_stats([tenant.id for tenant in tenants])
    return [_with_stats(tenant, usage.get(tenant.id, _NO_STATS)) for tenant in tenants]


async def list_company_stats(q: str | None) -> list[CompanyStats]:
    """Legacy unpaged list: newest first, or ranked top 50 when ``q`` is given."""
    query = _search_text(q)
    tenants = await (tenant_dao.search_by_name(query) if query else tenant_dao.list_ordered_by_created_at(desc=True))
    return await _stats_rows(tenants)


async def list_company_page(q: str | None, cursor: str | None, limit: int) -> CompanyPageOut:
    """Keyset page on ``created_at DESC, id DESC``; ``q`` returns a ranked top 50 with no cursor."""
    query = _search_text(q)
    if query:
        if cursor is not None:
            raise HTTPException(status_code=400, detail="Cursor cannot be combined with search")
        return CompanyPageOut(items=await _stats_rows(await tenant_dao.search_by_name(query)), next_cursor=None)
    page_size = min(max(limit, 1), MAX_PAGE_SIZE)
    position = _decode_cursor(cursor) if cursor is not None else None
    rows = await tenant_dao.list_page(
        limit=page_size, before=(position.created_at, position.id) if position is not None else None
    )
    page = rows[:page_size]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > page_size else None
    return CompanyPageOut(items=await _stats_rows(page), next_cursor=next_cursor)


async def list_company_options() -> list[CompanyOption]:
    """Every company, newest first, without stats."""
    return [
        CompanyOption.model_validate(tenant, from_attributes=True)
        for tenant in await tenant_dao.list_ordered_by_created_at(desc=True)
    ]


async def get_company_stats(company_id: uuid.UUID) -> CompanyStats | None:
    tenant = await tenant_dao.get(company_id)
    if tenant is None:
        return None
    return (await _stats_rows([tenant]))[0]
