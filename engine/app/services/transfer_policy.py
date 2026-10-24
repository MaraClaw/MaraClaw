from fastapi import HTTPException

from app.core.json_types import JsonObject
from app.dao.system_setting_dao import system_setting_dao
from app.db.session import connection_ctx
from app.records.system_setting import SystemSettingRecord
from app.records.user import UserRecord
from app.schemas.transfer_policy import TransferPolicy
from app.services.admin_audit import field_change, write_admin_audit

TRANSFER_POLICY_KEY = "transfer_policy"
DEFAULT_TRANSFER_POLICY = TransferPolicy(
    fee_amount_minor=2500,
    currency="USD",
    instructions=(
        "Contact the platform administrator for payment details. Include your account email and destination "
        "organization when paying. Your admission request is created only after payment is manually confirmed."
    ),
)


def _policy_value(policy: TransferPolicy) -> JsonObject:
    return {
        "fee_amount_minor": policy.fee_amount_minor,
        "currency": policy.currency,
        "instructions": policy.instructions,
    }


async def seed_transfer_policy() -> None:
    """Insert the initial policy once, without replacing an administrator's edits."""
    await system_setting_dao.insert_if_missing(TRANSFER_POLICY_KEY, _policy_value(DEFAULT_TRANSFER_POLICY))


async def _load_policy(*, for_update: bool = False) -> tuple[SystemSettingRecord, TransferPolicy]:
    setting = await system_setting_dao.get_by_key(TRANSFER_POLICY_KEY, for_update=for_update)
    if setting is None:
        raise HTTPException(status_code=503, detail="Transfer policy is not initialized")
    return setting, TransferPolicy.model_validate(setting.value)


async def get_transfer_policy() -> TransferPolicy:
    """Read the seeded policy; never invent defaults or mutate settings on reads."""
    _, policy = await _load_policy()
    return policy


async def update_transfer_policy(policy: TransferPolicy, actor: UserRecord, ip_address: str | None) -> TransferPolicy:
    """Serialize edits and their before/after audit on the same connection."""
    async with connection_ctx():
        setting, before = await _load_policy(for_update=True)
        value = _policy_value(policy)
        _ = await system_setting_dao.update(db_obj=setting, obj_in={"value": value})
        before_value = _policy_value(before)
        await write_admin_audit(
            actor=actor,
            action="transfer_policy_update",
            target_type="transfer_policy",
            changes={key: field_change(before_value[key], after) for key, after in value.items()},
            ip_address=ip_address,
        )
    return policy
