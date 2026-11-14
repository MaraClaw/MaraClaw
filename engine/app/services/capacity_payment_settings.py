"""Bank recipient details and the USD/VND rate for capacity orders (genesis platform admin only).

Stored as one ``system_settings`` row. The generic enterprise system-settings endpoints never serve or write this
key (``RESERVED_SETTING_KEYS``): it is readable and writable only through the genesis-only admin endpoints.
"""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from fastapi import HTTPException

from app.core.json_types import JsonObject, is_str_dict
from app.core.logging import logger
from app.core.security import load_user_principal
from app.dao.system_setting_dao import system_setting_dao
from app.db.session import connection_ctx
from app.records.capacity_order import MAX_USD_VND_RATE, CapacityRecipient
from app.records.user import UserRecord
from app.services.admin_audit import field_change, write_admin_audit
from app.services.admin_provisioning import is_genesis_platform_admin

SETTINGS_KEY: Final = "capacity_payment_settings"
RESERVED_SETTING_KEYS: Final = frozenset({"system_email_platform", SETTINGS_KEY})


@dataclass(frozen=True, slots=True)
class PaymentSettings:
    bank_name: str
    account_number: str
    branch_name: str
    usd_vnd_rate: int

    @property
    def recipient(self) -> CapacityRecipient:
        return CapacityRecipient(self.bank_name, self.account_number, self.branch_name)

    def as_json(self) -> JsonObject:
        return {**self.recipient.as_json(), "usd_vnd_rate": self.usd_vnd_rate}


def _parse(value: object) -> PaymentSettings | None:
    """The stored value as settings, or ``None`` when it is absent, partial or malformed (never configured)."""
    if not is_str_dict(value):
        return None
    texts = [value.get(name) for name in ("bank_name", "account_number", "branch_name")]
    rate = value.get("usd_vnd_rate")
    if not all(isinstance(text, str) and text.strip() for text in texts):
        return None
    if isinstance(rate, bool) or not isinstance(rate, int) or not 1 <= rate <= MAX_USD_VND_RATE:
        return None
    bank_name, account_number, branch_name = (str(text) for text in texts)
    return PaymentSettings(bank_name, account_number, branch_name, rate)


async def load_payment_settings() -> PaymentSettings | None:
    setting = await system_setting_dao.get_by_key(SETTINGS_KEY)
    if setting is None:
        return None
    parsed = _parse(setting.value)
    if parsed is None:
        logger.warning("[capacity] stored payment settings are incomplete; treating payments as not configured")
    return parsed


async def require_genesis_platform_admin(user_id: UUID) -> UserRecord:
    """Active genesis platform admin from fresh persisted membership and identity (never a token claim)."""
    user = await load_user_principal(user_id, fresh=True)
    if user.identity is None or not await is_genesis_platform_admin(user):
        raise HTTPException(403, "Only the genesis platform administrator may manage capacity payment settings")
    return user


async def read_settings_for_admin(user_id: UUID) -> PaymentSettings | None:
    async with connection_ctx():
        _ = await require_genesis_platform_admin(user_id)
        return await load_payment_settings()


async def update_payment_settings(
    user_id: UUID, settings: PaymentSettings, *, client_ip: str | None = None
) -> PaymentSettings:
    async with connection_ctx():
        _ = await require_genesis_platform_admin(user_id)
        before = await load_payment_settings()
        await system_setting_dao.insert_if_missing(SETTINGS_KEY, settings.as_json())
        stored = await system_setting_dao.get_by_key(SETTINGS_KEY, for_update=True)
        if stored is None:
            raise RuntimeError("Capacity payment settings row vanished under its own insert")
        actor = await require_genesis_platform_admin(user_id)
        _ = await system_setting_dao.update(db_obj=stored, obj_in={"value": settings.as_json()})
        before_json = before.as_json() if before is not None else {}
        after_json = settings.as_json()
        await write_admin_audit(
            actor=actor,
            action="capacity_payment_settings_update",
            target_type="capacity_payment_settings",
            changes={
                name: field_change(before_json.get(name), value)
                for name, value in after_json.items()
                if before_json.get(name) != value
            },
            ip_address=client_ip,
        )
        return settings
