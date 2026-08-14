"""Short-TTL Redis cache for tenant rows (no secrets)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.core.redis_cache import bump_version, cache_delete, cache_get_json, cache_key, cache_set_json, read_version
from app.records.tenant import TenantRecord


def _ttl() -> int:
    return int(getattr(get_settings(), "TENANT_CACHE_TTL_SECONDS", 60) or 0)


def _row_key(tenant_id: UUID) -> str:
    return cache_key("tenant", "v1", tenant_id)


def _ver_key(tenant_id: UUID) -> str:
    return cache_key("tenantver", tenant_id)


def _parse_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _dump(tenant: TenantRecord, ver: str) -> dict[str, Any]:
    return {
        "ver": ver,
        "id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
        "im_provider": tenant.im_provider,
        "im_config": tenant.im_config,
        "is_active": tenant.is_active,
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "default_message_limit": tenant.default_message_limit,
        "default_message_period": tenant.default_message_period,
        "default_max_agents": tenant.default_max_agents,
        "default_agent_ttl_hours": tenant.default_agent_ttl_hours,
        "default_max_llm_calls_per_day": tenant.default_max_llm_calls_per_day,
        "min_heartbeat_interval_minutes": tenant.min_heartbeat_interval_minutes,
        "timezone": tenant.timezone,
        "country_region": tenant.country_region,
        "sso_enabled": tenant.sso_enabled,
        "sso_domain": tenant.sso_domain,
        "default_max_triggers": tenant.default_max_triggers,
        "min_poll_interval_floor": tenant.min_poll_interval_floor,
        "max_webhook_rate_ceiling": tenant.max_webhook_rate_ceiling,
        "a2a_async_enabled": tenant.a2a_async_enabled,
        "default_model_id": str(tenant.default_model_id) if tenant.default_model_id else None,
        "is_system": tenant.is_system,
        "is_default_end_user_org": tenant.is_default_end_user_org,
    }


def _load(data: dict[str, Any]) -> TenantRecord | None:
    tenant_id = _parse_uuid(data.get("id"))
    if tenant_id is None or not data.get("name") or not data.get("slug"):
        return None
    return TenantRecord(
        id=tenant_id,
        name=str(data["name"]),
        slug=str(data["slug"]),
        im_provider=data.get("im_provider") or "web_only",
        im_config=data.get("im_config") if isinstance(data.get("im_config"), dict) else None,
        is_active=bool(data.get("is_active", True)),
        created_at=_parse_dt(data.get("created_at")),
        default_message_limit=int(data.get("default_message_limit") or 50),
        default_message_period=data.get("default_message_period") or "permanent",
        default_max_agents=int(data.get("default_max_agents") or 2),
        default_agent_ttl_hours=int(data.get("default_agent_ttl_hours") or 0),
        default_max_llm_calls_per_day=int(data.get("default_max_llm_calls_per_day") or 1000),
        min_heartbeat_interval_minutes=int(data.get("min_heartbeat_interval_minutes") or 240),
        timezone=data.get("timezone") or "UTC",
        country_region=data.get("country_region") or "001",
        sso_enabled=bool(data.get("sso_enabled", False)),
        sso_domain=data.get("sso_domain"),
        default_max_triggers=int(data.get("default_max_triggers") or 20),
        min_poll_interval_floor=int(data.get("min_poll_interval_floor") or 5),
        max_webhook_rate_ceiling=int(data.get("max_webhook_rate_ceiling") or 5),
        a2a_async_enabled=bool(data.get("a2a_async_enabled", True)),
        default_model_id=_parse_uuid(data.get("default_model_id")),
        is_system=bool(data.get("is_system", False)),
        is_default_end_user_org=bool(data.get("is_default_end_user_org", False)),
    )


async def get_cached_tenant(tenant_id: UUID) -> TenantRecord | None:
    if _ttl() <= 0:
        return None
    payload = await cache_get_json(_row_key(tenant_id))
    if not isinstance(payload, dict):
        return None
    current = await read_version(_ver_key(tenant_id))
    if str(payload.get("ver") or "0") != current:
        return None
    return _load(payload)


async def peek_tenant_version(tenant_id: UUID) -> str:
    if _ttl() <= 0:
        return "0"
    return await read_version(_ver_key(tenant_id))


async def set_cached_tenant(tenant: TenantRecord, *, observed_ver: str | None = None) -> None:
    if _ttl() <= 0:
        return
    ver = await read_version(_ver_key(tenant.id))
    if observed_ver is not None and ver != observed_ver:
        return
    await cache_set_json(_row_key(tenant.id), _dump(tenant, ver), ttl=_ttl())


async def bump_tenant_cache(tenant_id: UUID | None) -> None:
    if tenant_id is None or _ttl() <= 0:
        return
    await bump_version(_ver_key(tenant_id), ttl=_ttl() * 20)
    await cache_delete(_row_key(tenant_id))
