"""Tenant records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    is_system: bool = False
    is_default_end_user_org: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TenantRecord:
        im_config = row.get("im_config")
        if im_config is not None and not isinstance(im_config, dict):
            im_config = dict(im_config)
        return cls(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            im_provider=row.get("im_provider") or "web_only",
            im_config=im_config,
            is_active=bool(row.get("is_active", True)),
            created_at=row.get("created_at"),
            default_message_limit=int(row.get("default_message_limit") or 50),
            default_message_period=row.get("default_message_period") or "permanent",
            default_max_agents=int(row.get("default_max_agents") or 2),
            default_agent_ttl_hours=int(row.get("default_agent_ttl_hours") or 0),
            default_max_llm_calls_per_day=int(row.get("default_max_llm_calls_per_day") or 1000),
            min_heartbeat_interval_minutes=int(row.get("min_heartbeat_interval_minutes") or 240),
            timezone=row.get("timezone") or "UTC",
            country_region=row.get("country_region") or "001",
            sso_enabled=bool(row.get("sso_enabled", False)),
            sso_domain=row.get("sso_domain"),
            default_max_triggers=int(row.get("default_max_triggers") or 20),
            min_poll_interval_floor=int(row.get("min_poll_interval_floor") or 5),
            max_webhook_rate_ceiling=int(row.get("max_webhook_rate_ceiling") or 5),
            a2a_async_enabled=bool(row.get("a2a_async_enabled", True)),
            default_model_id=row.get("default_model_id"),
            is_system=bool(row.get("is_system", False)),
            is_default_end_user_org=bool(row.get("is_default_end_user_org", False)),
        )

    @property
    def logo_url(self) -> str | None:
        if isinstance(self.im_config, dict):
            value = self.im_config.get("logo_url")
            return value if isinstance(value, str) and value else None
        return None
