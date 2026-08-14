"""DAO for okr_settings (psycopg)."""

from __future__ import annotations

from typing import ClassVar, final
from uuid import UUID

from app.dao.base import BaseDAO
from app.db.errors import UniqueViolationError
from app.records.okr import OKRSettingsRecord

_COLUMNS = (
    "tenant_id",
    "enabled",
    "first_enabled_at",
    "daily_report_enabled",
    "daily_report_time",
    "daily_report_skip_non_workdays",
    "weekly_report_enabled",
    "weekly_report_day",
    "period_frequency",
    "period_length_days",
    "okr_agent_id",
)


@final
class OKRSettingsDAO(BaseDAO[OKRSettingsRecord]):
    """Minimal DAO for per-tenant OKR configuration."""

    table: ClassVar[str] = "okr_settings"
    pk: ClassVar[str] = "tenant_id"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(OKRSettingsRecord.from_row)

    async def get_by_tenant(self, tenant_id: UUID) -> OKRSettingsRecord | None:
        """Fetch OKR settings for a tenant."""
        return await self.get(tenant_id)

    async def get_or_create(self, tenant_id: UUID) -> OKRSettingsRecord:
        """Return settings for a tenant, creating a default row when missing."""
        existing = await self.get(tenant_id)
        if existing is not None:
            return existing
        try:
            return await self.create(obj_in={"tenant_id": tenant_id})
        except UniqueViolationError:
            refreshed = await self.get(tenant_id)
            if refreshed is None:
                raise
            return refreshed


okr_settings_dao = OKRSettingsDAO()
