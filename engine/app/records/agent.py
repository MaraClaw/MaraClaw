"""Agent and agent-permission records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import (
    datetime_from_row,
    int_from_row,
    mapping_from_row,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)


@dataclass(slots=True)
class AgentPermissionRecord:
    """Access permission row for a digital employee."""

    id: UUID
    agent_id: UUID
    scope_type: str
    scope_id: UUID | None = None
    access_level: str = "use"

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentPermissionRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            scope_type=str_from_row(row["scope_type"]),
            scope_id=uuid_from_row_opt(row.get("scope_id")),
            access_level=str_from_row(row.get("access_level"), "use") or "use",
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
    def from_row(cls, row: Mapping[str, object]) -> AgentRecord:
        policy = mapping_from_row(row.get("autonomy_policy") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            name=str_from_row(row["name"]),
            creator_id=uuid_from_row(row["creator_id"]),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            avatar_url=str_from_row(row["avatar_url"]) or None,
            role_description=str_from_row(row.get("role_description")),
            bio=str_from_row(row["bio"]) or None,
            welcome_message=str_from_row(row["welcome_message"]) or None,
            agent_type=str_from_row(row.get("agent_type"), "native") or "native",
            gogcli_enabled=bool(row.get("gogcli_enabled", False)),
            api_key_hash=str_from_row(row["api_key_hash"]) or None,
            openclaw_last_seen=datetime_from_row(row.get("openclaw_last_seen")),
            status=str_from_row(row.get("status"), "creating") or "creating",
            container_id=str_from_row(row["container_id"]) or None,
            container_port=int_from_row(row["container_port"]) if row.get("container_port") is not None else None,
            primary_model_id=uuid_from_row_opt(row.get("primary_model_id")),
            fallback_model_id=uuid_from_row_opt(row.get("fallback_model_id")),
            autonomy_policy=policy,
            max_tokens_per_day=int_from_row(row["max_tokens_per_day"]) if row.get("max_tokens_per_day") is not None else None,
            max_tokens_per_month=int_from_row(row["max_tokens_per_month"])
            if row.get("max_tokens_per_month") is not None
            else None,
            tokens_used_today=int_from_row(row.get("tokens_used_today")),
            tokens_used_month=int_from_row(row.get("tokens_used_month")),
            last_daily_reset=datetime_from_row(row.get("last_daily_reset")),
            last_monthly_reset=datetime_from_row(row.get("last_monthly_reset")),
            tokens_used_total=int_from_row(row.get("tokens_used_total")),
            cache_read_tokens_today=int_from_row(row.get("cache_read_tokens_today")),
            cache_read_tokens_month=int_from_row(row.get("cache_read_tokens_month")),
            cache_read_tokens_total=int_from_row(row.get("cache_read_tokens_total")),
            cache_creation_tokens_today=int_from_row(row.get("cache_creation_tokens_today")),
            cache_creation_tokens_month=int_from_row(row.get("cache_creation_tokens_month")),
            cache_creation_tokens_total=int_from_row(row.get("cache_creation_tokens_total")),
            context_window_size=int_from_row(row.get("context_window_size"), 100),
            max_tool_rounds=int_from_row(row.get("max_tool_rounds"), 50),
            max_triggers=int_from_row(row.get("max_triggers"), 20),
            min_poll_interval_min=int_from_row(row.get("min_poll_interval_min"), 5),
            webhook_rate_limit=int_from_row(row.get("webhook_rate_limit"), 5),
            expires_at=datetime_from_row(row.get("expires_at")),
            is_expired=bool(row.get("is_expired", False)),
            is_system=bool(row.get("is_system", False)),
            access_mode=str_from_row(row.get("access_mode"), "company") or "company",
            company_access_level=str_from_row(row.get("company_access_level"), "use") or "use",
            llm_calls_today=int_from_row(row.get("llm_calls_today")),
            max_llm_calls_per_day=int_from_row(row.get("max_llm_calls_per_day"), 1000),
            llm_calls_reset_at=datetime_from_row(row.get("llm_calls_reset_at")),
            template_id=uuid_from_row_opt(row.get("template_id")),
            heartbeat_enabled=bool(row.get("heartbeat_enabled", True)),
            heartbeat_interval_minutes=int_from_row(row.get("heartbeat_interval_minutes"), 240),
            heartbeat_active_hours=str_from_row(row.get("heartbeat_active_hours"), "09:00-18:00") or "09:00-18:00",
            last_heartbeat_at=datetime_from_row(row.get("last_heartbeat_at")),
            timezone=str_from_row(row["timezone"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
            last_active_at=datetime_from_row(row.get("last_active_at")),
        )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_hash)
