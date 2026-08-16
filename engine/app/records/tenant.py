"""Tenant records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
class TenantRecord:
    """Company / organization isolation boundary."""

    id: UUID
    name: str
    slug: str
    im_provider: str = "web_only"
    im_config: dict[str, Any] | None = None
    is_active: bool = True
    created_at: datetime | None = None
    default_message_limit: int = 50
    default_message_period: str = "permanent"
    default_max_agents: int = 2
    default_agent_ttl_hours: int = 0
    default_max_llm_calls_per_day: int = 1000
    min_heartbeat_interval_minutes: int = 240
    timezone: str = "UTC"
    country_region: str = "001"
    sso_enabled: bool = False
    sso_domain: str | None = None
    default_max_triggers: int = 20
    min_poll_interval_floor: int = 5
    max_webhook_rate_ceiling: int = 5
    a2a_async_enabled: bool = True
    default_model_id: UUID | None = None
    default_fallback_model_id: UUID | None = None
    is_system: bool = False
    is_default_end_user_org: bool = False

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> TenantRecord:
        im_config = mapping_from_row(row.get("im_config")) or None
        return cls(
            id=uuid_from_row(row["id"]),
            name=str_from_row(row["name"]),
            slug=str_from_row(row["slug"]),
            im_provider=str_from_row(row.get("im_provider"), "web_only") or "web_only",
            im_config=im_config,
            is_active=bool(row.get("is_active", True)),
            created_at=datetime_from_row(row.get("created_at")),
            default_message_limit=int_from_row(row.get("default_message_limit"), 50),
            default_message_period=str_from_row(row.get("default_message_period"), "permanent") or "permanent",
            default_max_agents=int_from_row(row.get("default_max_agents"), 2),
            default_agent_ttl_hours=int_from_row(row.get("default_agent_ttl_hours")),
            default_max_llm_calls_per_day=int_from_row(row.get("default_max_llm_calls_per_day"), 1000),
            min_heartbeat_interval_minutes=int_from_row(row.get("min_heartbeat_interval_minutes"), 240),
            timezone=str_from_row(row.get("timezone"), "UTC") or "UTC",
            country_region=str_from_row(row.get("country_region"), "001") or "001",
            sso_enabled=bool(row.get("sso_enabled", False)),
            sso_domain=str_from_row(row["sso_domain"]) or None,
            default_max_triggers=int_from_row(row.get("default_max_triggers"), 20),
            min_poll_interval_floor=int_from_row(row.get("min_poll_interval_floor"), 5),
            max_webhook_rate_ceiling=int_from_row(row.get("max_webhook_rate_ceiling"), 5),
            a2a_async_enabled=bool(row.get("a2a_async_enabled", True)),
            default_model_id=uuid_from_row_opt(row.get("default_model_id")),
            default_fallback_model_id=uuid_from_row_opt(row.get("default_fallback_model_id")),
            is_system=bool(row.get("is_system", False)),
            is_default_end_user_org=bool(row.get("is_default_end_user_org", False)),
        )

    @property
    def logo_url(self) -> str | None:
        if isinstance(self.im_config, dict):
            value = self.im_config.get("logo_url")
            return value if isinstance(value, str) and value else None
        return None
