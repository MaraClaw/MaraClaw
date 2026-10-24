"""DAO for system_settings table"""

from __future__ import annotations

from typing import ClassVar, final

from app.core.json_types import JsonObject, is_str_dict
from app.dao.base import BaseDAO
from app.db.types import as_jsonb
from app.records.system_setting import SystemSettingRecord

_COLUMNS = ("key", "value", "updated_at")


@final
class SystemSettingDAO(BaseDAO[SystemSettingRecord]):
    """Typed access layer for platform-level system settings."""

    table: ClassVar[str] = "system_settings"
    pk: ClassVar[str] = "key"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(SystemSettingRecord.from_row)

    async def get_by_key(self, key: str, *, for_update: bool = False) -> SystemSettingRecord | None:
        lock = " FOR UPDATE" if for_update else ""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM system_settings WHERE key = %(key)s{lock}",
                {"key": key},
            )
            return SystemSettingRecord.from_row(row) if row is not None else None

    async def insert_if_missing(self, key: str, value: JsonObject) -> None:
        """Atomically preserve existing values, including concurrent inserts."""
        async with self.session() as db:
            await db.execute(
                "INSERT INTO system_settings (key, value) VALUES (%(key)s, %(value)s) ON CONFLICT (key) DO NOTHING",
                {"key": key, "value": as_jsonb(value)},
            )

    async def get_value(self, key: str, default: object = None) -> object:
        setting = await self.get_by_key(key)
        if setting is None:
            return default
        return setting.value

    async def is_invitation_code_enabled(self) -> bool:
        value = await self.get_value("invitation_code_enabled", {})
        if not isinstance(value, dict):
            return False
        return bool(value.get("enabled", False))

    async def is_sso_custom_domain_redirect_enabled(self) -> bool:
        value = await self.get_value("sso_custom_domain_redirect_enabled", {})
        if not isinstance(value, dict):
            return True
        return bool(value.get("enabled", True))

    async def is_flag_enabled(self, key: str, *, default: bool = True) -> bool:
        value = await self.get_value(key, None)
        if value is None:
            return default
        if not is_str_dict(value):
            return default
        enabled = value.get("enabled")
        if isinstance(enabled, bool):
            return enabled
        return default

    async def set_flag(self, key: str, enabled: bool) -> SystemSettingRecord:
        existing = await self.get_by_key(key)
        if existing is not None:
            return await self.update(db_obj=existing, obj_in={"value": {"enabled": enabled}})
        return await self.create(obj_in={"key": key, "value": {"enabled": enabled}})


system_setting_dao = SystemSettingDAO()
