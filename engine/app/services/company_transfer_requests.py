from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never
from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_tenants, lock_user
from app.dao.company_admission_dao import company_admission_dao as admissions
from app.dao.company_transfer_dao import company_transfer_dao as transfers
from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod, AdmissionStatus, CompanyAdmissionRecord
from app.records.company_transfer import CompanyTransferRecord, TransferChoice
from app.records.user import UserRecord
from app.services.company_admission_auto import admit_with_invitation, submit_or_admit
from app.services.company_admissions import fresh_user, validate_method
from app.services.company_transfer_fence import fence_owned_agents
from app.services.company_transfer_guards import require_default_source
from app.services.transfer_policy import get_transfer_policy


@dataclass(frozen=True, slots=True)
class TransferDestination:
    tenant_id: UUID
    method: AdmissionMethod
    invitation_code: str | None = None


@dataclass(frozen=True, slots=True)
class TransferSelection:
    choice: TransferChoice
    free_destructive_consent: bool = False


async def submit_transfer(
    applicant: UserRecord, destination: TransferDestination, selection: TransferSelection
) -> CompanyTransferRecord | CompanyAdmissionRecord:
    async with connection_ctx() as db:
        # Invitation admission moves assets, so the agent fence precedes every tenant/user lock.
        fenced_ids = (
            await fence_owned_agents(applicant)
            if destination.method == AdmissionMethod.INVITATION and selection.choice == TransferChoice.FREE
            else None
        )
        await lock_tenants(db, (applicant.tenant_id, destination.tenant_id))
        await lock_user(db, applicant.id, applicant.tenant_id)
        current = await fresh_user(applicant.id)
        await require_default_source(current)
        if current.is_genesis or current.is_platform_admin or current.role == "org_admin":
            raise HTTPException(403, "Admin membership cannot be converted")
        if current.tenant_id is None:
            raise HTTPException(409, "A default-organization membership is required for a transfer")
        target = await admissions.tenant(destination.tenant_id)
        if target is None or not target.is_active or target.is_system or target.is_default_end_user_org:
            raise HTTPException(400, "Target must be an active company")
        if current.tenant_id == target.id:
            raise HTTPException(409, "You already belong to that company")
        await validate_method(current, target.id, destination.method, destination.invitation_code)
        match selection.choice:
            case TransferChoice.FREE:
                if not selection.free_destructive_consent:
                    raise HTTPException(422, {"code": "free_destructive_consent_required"})
            case TransferChoice.PAID:
                pass
            case unreachable:
                assert_never(unreachable)
        active = await transfers.active_for(current.id)
        if active is not None:
            if active.target_tenant_id != target.id or active.choice != selection.choice:
                raise HTTPException(409, {"code": "transfer_already_pending", "transfer_request_id": str(active.id)})
            if active.admission_request_id is not None:
                admission = await admissions.get(active.admission_request_id)
                if admission is None:
                    raise HTTPException(409, "Transfer admission is unavailable")
                if (
                    admission.status == AdmissionStatus.PENDING
                    and destination.method == AdmissionMethod.INVITATION
                    and destination.invitation_code is not None
                    and fenced_ids is not None
                ):
                    return await admit_with_invitation(
                        current, target.id, destination.invitation_code, transfer_id=active.id, fenced_ids=fenced_ids
                    )
                return admission
            return active
        if await admissions.pending_for(current.id) is not None:
            raise HTTPException(409, "An admission request is already pending; withdraw it before choosing a transfer")
        policy = await get_transfer_policy()
        transfer = await transfers.create(
            obj_in={
                "applicant_user_id": current.id,
                "source_tenant_id": current.tenant_id,
                "target_tenant_id": target.id,
                "choice": selection.choice.value,
                "method": destination.method.value,
                "invitation_code": destination.invitation_code,
                "fee_amount_minor": policy.fee_amount_minor,
                "currency": policy.currency,
                "instructions": policy.instructions,
                "free_consent_at": datetime.now(UTC) if selection.choice == TransferChoice.FREE else None,
            }
        )
        match selection.choice:
            case TransferChoice.PAID:
                return transfer
            case TransferChoice.FREE:
                admission = await submit_or_admit(
                    current,
                    target.id,
                    destination.method,
                    invitation_code=destination.invitation_code,
                    transfer_id=transfer.id,
                    fenced_ids=fenced_ids,
                )
                await transfers.update(
                    db_obj=transfer, obj_in={"status": "admission_created", "admission_request_id": admission.id}
                )
                return admission
            case unreachable:
                assert_never(unreachable)
