"""Short-TTL Redis cache for tenant rows (no secrets)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.config import get_settings
from app.core.json_types import (
    is_json_object,
    json_as_bool,
    json_as_int,
    json_as_str,
    json_as_str_or,
    json_object_from,
    mapping_from_row,
)
from app.core.redis_cache import bump_version, cache_delete, cache_get_json, cache_key, cache_set_json, read_version
from app.records.tenant import TenantRecord


def _ttl() -> int:
    return get_settings().TENANT_CACHE_TTL_SECONDS or 0


def _row_key(tenant_id: UUID) -> str:
    return cache_key("tenant", "v1", tenant_id)


def _ver_key(tenant_id: UUID) -> str:
    return cache_key("tenantver", tenant_id)


def _parse_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _parse_dt(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _dump(tenant: TenantRecord, ver: str) -> dict[str, object]:
    return {
        "ver": ver,
        "id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
        "im_provider": tenant.im_provider,
        "im_config": json_object_from(tenant.im_config) if tenant.im_config else None,
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
        "default_fallback_model_id": (
            str(tenant.default_fallback_model_id) if tenant.default_fallback_model_id else None
        ),
        "default_secondary_model_id": (
            str(tenant.default_secondary_model_id) if tenant.default_secondary_model_id else None
        ),
        "is_system": tenant.is_system,
        "is_default_end_user_org": tenant.is_default_end_user_org,
    }


def _load(data: object) -> TenantRecord | None:
    payload = json_object_from(data)
    tenant_id = _parse_uuid(payload.get("id"))
    name = json_as_str(payload.get("name"))
    slug = json_as_str(payload.get("slug"))
    if tenant_id is None or not name or not slug:
        return None
    im_raw = payload.get("im_config")
    return TenantRecord(
        id=tenant_id,
        name=name,
        slug=slug,
        im_provider=json_as_str_or(payload.get("im_provider"), "web_only") or "web_only",
        im_config=mapping_from_row(im_raw) if is_json_object(im_raw) else None,
        is_active=json_as_bool(payload.get("is_active"), True),
        created_at=_parse_dt(payload.get("created_at")),
        default_message_limit=json_as_int(payload.get("default_message_limit"), 50),
        default_message_period=json_as_str_or(payload.get("default_message_period"), "permanent") or "permanent",
        default_max_agents=json_as_int(payload.get("default_max_agents"), 2),
        default_agent_ttl_hours=json_as_int(payload.get("default_agent_ttl_hours")),
        default_max_llm_calls_per_day=json_as_int(payload.get("default_max_llm_calls_per_day"), 1000),
        min_heartbeat_interval_minutes=json_as_int(payload.get("min_heartbeat_interval_minutes"), 240),
        timezone=json_as_str_or(payload.get("timezone"), "UTC") or "UTC",
        country_region=json_as_str_or(payload.get("country_region"), "001") or "001",
        sso_enabled=json_as_bool(payload.get("sso_enabled")),
        sso_domain=json_as_str(payload.get("sso_domain")),
        default_max_triggers=json_as_int(payload.get("default_max_triggers"), 20),
        min_poll_interval_floor=json_as_int(payload.get("min_poll_interval_floor"), 5),
        max_webhook_rate_ceiling=json_as_int(payload.get("max_webhook_rate_ceiling"), 5),
        a2a_async_enabled=json_as_bool(payload.get("a2a_async_enabled"), True),
        default_model_id=_parse_uuid(payload.get("default_model_id")),
        default_fallback_model_id=_parse_uuid(payload.get("default_fallback_model_id")),
        default_secondary_model_id=_parse_uuid(payload.get("default_secondary_model_id")),
        is_system=json_as_bool(payload.get("is_system")),
        is_default_end_user_org=json_as_bool(payload.get("is_default_end_user_org")),
    )


async def get_cached_tenant(tenant_id: UUID) -> TenantRecord | None:
    if _ttl() <= 0:
        return None
    cached: object = await cache_get_json(_row_key(tenant_id))
    if not is_json_object(cached):
        return None
    current = await read_version(_ver_key(tenant_id))
    if str(cached.get("ver") or "0") != current:
        return None
    return _load(cached)


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
    _ = await cache_set_json(_row_key(tenant.id), _dump(tenant, ver), ttl=_ttl())


async def bump_tenant_cache(tenant_id: UUID | None) -> None:
    if tenant_id is None or _ttl() <= 0:
        return
    await bump_version(_ver_key(tenant_id), ttl=_ttl() * 20)
    await cache_delete(_row_key(tenant_id))
