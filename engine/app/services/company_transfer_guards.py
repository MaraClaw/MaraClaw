from typing import assert_never
from uuid import UUID

from fastapi import HTTPException

from app.dao.company_admission_dao import company_admission_dao as admissions
from app.dao.company_transfer_dao import company_transfer_dao as transfers
from app.db.session import connection_ctx
from app.records.company_transfer import TransferChoice, TransferStatus
from app.records.user import UserRecord
from app.schemas.company_transfer import TransferQuote
from app.services.transfer_policy import get_transfer_policy


async def has_owned_agents(user: UserRecord) -> bool:
    async with connection_ctx() as db:
        return (
            await db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM agents WHERE creator_id=%(user)s "
                "AND tenant_id IS NOT DISTINCT FROM %(tenant)s)",
                {"user": user.id, "tenant": user.tenant_id},
            )
            is True
        )


async def require_default_source(user: UserRecord) -> None:
    if user.tenant_id is None:
        return
    source = await admissions.tenant(user.tenant_id)
    if source is None or not source.is_active or not source.is_default_end_user_org:
        raise HTTPException(409, {"code": "transfer_source_forbidden"})


async def require_admission_transfer(user: UserRecord, target_id: UUID, transfer_id: UUID | None) -> None:
    await require_default_source(user)
    active = await transfers.active_for(user.id)
    if transfer_id is None:
        if active is not None:
            raise HTTPException(409, {"code": "transfer_already_pending", "transfer_request_id": str(active.id)})
        if await has_owned_agents(user):
            raise HTTPException(409, {"code": "transfer_choice_required"})
        return
    transfer = await transfers.get_locked(transfer_id)
    if (
        transfer is None
        or transfer.applicant_user_id != user.id
        or transfer.source_tenant_id != user.tenant_id
        or transfer.target_tenant_id != target_id
        or transfer.status in {TransferStatus.REJECTED, TransferStatus.WITHDRAWN}
        or (active is not None and active.id != transfer_id)
    ):
        raise HTTPException(409, {"code": "transfer_request_mismatch"})
    match transfer.choice:
        case TransferChoice.PAID:
            if transfer.payment_confirmed_at is None:
                raise HTTPException(409, {"code": "payment_review_required"})
        case TransferChoice.FREE:
            if transfer.free_consent_at is None:
                raise HTTPException(409, {"code": "free_destructive_consent_required"})
        case unreachable:
            assert_never(unreachable)


async def quote(user: UserRecord) -> TransferQuote:
    policy = await get_transfer_policy()
    source = await admissions.tenant(user.tenant_id) if user.tenant_id is not None else None
    eligible = source is not None and source.is_active and source.is_default_end_user_org
    assets = await has_owned_agents(user)
    return TransferQuote(
        fee_amount_minor=policy.fee_amount_minor,
        currency=policy.currency,
        instructions=policy.instructions,
        source_eligible=eligible,
        has_owned_agents=assets,
        explicit_choice_required=eligible and assets,
    )
