"""Timezone utilities for resolving agent and tenant timezones."""

import uuid
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo


class _HasTimezone(Protocol):
    timezone: str | None

# Common timezones for frontend dropdown
COMMON_TIMEZONES = [
    "UTC",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Asia/Singapore",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Moscow",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Australia/Sydney",
    "Pacific/Auckland",
]


async def get_agent_timezone(agent_id: uuid.UUID) -> str:
    """Resolve effective timezone for an agent.

    Priority: agent.timezone → tenant.timezone → 'UTC'
    """
    from app.dao.agent_dao import agent_dao
    from app.dao.tenant_dao import tenant_dao

    agent = await agent_dao.get(agent_id)
    if not agent:
        return "UTC"

    # Agent-level override
    if agent.timezone:
        return agent.timezone

    # Tenant-level default
    if agent.tenant_id:
        tenant = await tenant_dao.get(agent.tenant_id)
        if tenant and tenant.timezone:
            return tenant.timezone

    return "UTC"


def get_agent_timezone_sync(agent: _HasTimezone, tenant: object | None = None) -> str:
    """Synchronous version - when agent and tenant objects are already loaded.

    Priority: agent.timezone → tenant.timezone → 'UTC'
    """
    if agent.timezone:
        return agent.timezone
    from app.core.json_types import object_attr

    tenant_timezone = object_attr(tenant, "timezone") if tenant is not None else None
    if isinstance(tenant_timezone, str) and tenant_timezone:
        return tenant_timezone
    return "UTC"


def now_in_timezone(tz_name: str) -> datetime:
    """Get current datetime in the given timezone."""
    try:
        tz = ZoneInfo(tz_name)
    except KeyError, Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz)
