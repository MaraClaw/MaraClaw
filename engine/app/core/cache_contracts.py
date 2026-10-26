from dataclasses import dataclass
from typing import TypedDict
from uuid import UUID

from app.core.json_types import JsonObject


@dataclass(frozen=True, slots=True)
class RedisValueRead:
    value: str | None


@dataclass(frozen=True, slots=True)
class SessionCacheObservation:
    identity_id: UUID | None
    user_version: str
    identity_version: str


class SessionIdentitySnapshot(TypedDict):
    id: str
    email: str | None
    phone: str | None
    is_active: bool
    is_platform_admin: bool
    email_verified: bool
    must_change_password: bool
    created_at: str | None
    updated_at: str | None


class SessionUserSnapshot(TypedDict):
    id: str
    identity_id: str | None
    tenant_id: str | None
    display_name: str
    avatar_url: str | None
    title: str | None
    timezone: str | None
    role: str
    is_active: bool
    registration_source: str | None
    created_at: str | None
    updated_at: str | None
    quota_message_limit: int
    quota_message_period: str
    quota_max_agents: int
    quota_agent_ttl_hours: int
    is_genesis: bool


class SessionSnapshot(TypedDict):
    u_ver: str
    i_ver: str
    user: SessionUserSnapshot
    identity: SessionIdentitySnapshot | None


class TenantSnapshot(TypedDict):
    ver: str
    id: str
    name: str
    slug: str
    im_provider: str
    im_config: JsonObject | None
    is_active: bool
    created_at: str | None
    default_message_limit: int
    default_message_period: str
    default_max_agents: int
    default_agent_ttl_hours: int
    default_max_llm_calls_per_day: int
    min_heartbeat_interval_minutes: int
    timezone: str
    country_region: str
    sso_enabled: bool
    sso_domain: str | None
    default_max_triggers: int
    min_poll_interval_floor: int
    max_webhook_rate_ceiling: int
    a2a_async_enabled: bool
    default_model_id: str | None
    default_fallback_model_id: str | None
    default_secondary_model_id: str | None
    is_system: bool
    is_default_end_user_org: bool
