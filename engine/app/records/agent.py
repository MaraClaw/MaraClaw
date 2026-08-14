"""Agent and agent-permission records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import mapping_from_row


@dataclass(slots=True)
class AgentPermissionRecord:
    """Access permission row for a digital employee."""

    id: UUID
    agent_id: UUID
    scope_type: str
    scope_id: UUID | None = None
    access_level: str = "use"

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentPermissionRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            scope_type=row["scope_type"],
            scope_id=row.get("scope_id"),
            access_level=row.get("access_level") or "use",
        )


@dataclass(slots=True)
class AgentRecord:
    """Digital employee instance."""

    id: UUID
    name: str
    creator_id: UUID
    tenant_id: UUID | None = None
    avatar_url: str | None = None
    role_description: str = ""
    bio: str | None = None
    welcome_message: str | None = None
    agent_type: str = "native"
    gogcli_enabled: bool = False
    api_key_hash: str | None = None
    openclaw_last_seen: datetime | None = None
    status: str = "creating"
    container_id: str | None = None
    container_port: int | None = None
    primary_model_id: UUID | None = None
    fallback_model_id: UUID | None = None
    autonomy_policy: dict[str, Any] = field(default_factory=dict[str, Any])
    max_tokens_per_day: int | None = None
    max_tokens_per_month: int | None = None
    tokens_used_today: int = 0
    tokens_used_month: int = 0
    last_daily_reset: datetime | None = None
    last_monthly_reset: datetime | None = None
    tokens_used_total: int = 0
    cache_read_tokens_today: int = 0
    cache_read_tokens_month: int = 0
    cache_read_tokens_total: int = 0
    cache_creation_tokens_today: int = 0
    cache_creation_tokens_month: int = 0
    cache_creation_tokens_total: int = 0
    context_window_size: int = 100
    max_tool_rounds: int = 50
    max_triggers: int = 20
    min_poll_interval_min: int = 5
    webhook_rate_limit: int = 5
    expires_at: datetime | None = None
    is_expired: bool = False
    is_system: bool = False
    access_mode: str = "company"
    company_access_level: str = "use"
    llm_calls_today: int = 0
    max_llm_calls_per_day: int = 1000
    llm_calls_reset_at: datetime | None = None
    template_id: UUID | None = None
    heartbeat_enabled: bool = True
    heartbeat_interval_minutes: int = 240
    heartbeat_active_hours: str = "09:00-18:00"
    last_heartbeat_at: datetime | None = None
    timezone: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_active_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentRecord:
        policy = mapping_from_row(row.get("autonomy_policy") or {})
        return cls(
            id=row["id"],
            name=row["name"],
            creator_id=row["creator_id"],
            tenant_id=row.get("tenant_id"),
            avatar_url=row.get("avatar_url"),
            role_description=row.get("role_description") or "",
            bio=row.get("bio"),
            welcome_message=row.get("welcome_message"),
            agent_type=row.get("agent_type") or "native",
            gogcli_enabled=bool(row.get("gogcli_enabled", False)),
            api_key_hash=row.get("api_key_hash"),
            openclaw_last_seen=row.get("openclaw_last_seen"),
            status=row.get("status") or "creating",
            container_id=row.get("container_id"),
            container_port=row.get("container_port"),
            primary_model_id=row.get("primary_model_id"),
            fallback_model_id=row.get("fallback_model_id"),
            autonomy_policy=policy,
            max_tokens_per_day=row.get("max_tokens_per_day"),
            max_tokens_per_month=row.get("max_tokens_per_month"),
            tokens_used_today=int(row.get("tokens_used_today") or 0),
            tokens_used_month=int(row.get("tokens_used_month") or 0),
            last_daily_reset=row.get("last_daily_reset"),
            last_monthly_reset=row.get("last_monthly_reset"),
            tokens_used_total=int(row.get("tokens_used_total") or 0),
            cache_read_tokens_today=int(row.get("cache_read_tokens_today") or 0),
            cache_read_tokens_month=int(row.get("cache_read_tokens_month") or 0),
            cache_read_tokens_total=int(row.get("cache_read_tokens_total") or 0),
            cache_creation_tokens_today=int(row.get("cache_creation_tokens_today") or 0),
            cache_creation_tokens_month=int(row.get("cache_creation_tokens_month") or 0),
            cache_creation_tokens_total=int(row.get("cache_creation_tokens_total") or 0),
            context_window_size=int(row.get("context_window_size") or 100),
            max_tool_rounds=int(row.get("max_tool_rounds") or 50),
            max_triggers=int(row.get("max_triggers") or 20),
            min_poll_interval_min=int(row.get("min_poll_interval_min") or 5),
            webhook_rate_limit=int(row.get("webhook_rate_limit") or 5),
            expires_at=row.get("expires_at"),
            is_expired=bool(row.get("is_expired", False)),
            is_system=bool(row.get("is_system", False)),
            access_mode=row.get("access_mode") or "company",
            company_access_level=row.get("company_access_level") or "use",
            llm_calls_today=int(row.get("llm_calls_today") or 0),
            max_llm_calls_per_day=int(row.get("max_llm_calls_per_day") or 1000),
            llm_calls_reset_at=row.get("llm_calls_reset_at"),
            template_id=row.get("template_id"),
            heartbeat_enabled=bool(row.get("heartbeat_enabled", True)),
            heartbeat_interval_minutes=int(row.get("heartbeat_interval_minutes") or 240),
            heartbeat_active_hours=row.get("heartbeat_active_hours") or "09:00-18:00",
            last_heartbeat_at=row.get("last_heartbeat_at"),
            timezone=row.get("timezone"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            last_active_at=row.get("last_active_at"),
        )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_hash)
