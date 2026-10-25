from datetime import UTC, datetime
from typing import assert_never
from uuid import UUID

from fastapi import HTTPException

from app.api.background_tasks import schedule_background_task
from app.core.acl_locks import lock_tenants, lock_user
from app.core.notification_kinds import NotificationKind
from app.dao.company_transfer_dao import company_transfer_dao as transfers
from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod, AdmissionStatus
from app.records.company_transfer import CompanyTransferRecord, TransferChoice, TransferStatus
from app.records.user import UserRecord
from app.schemas.company_transfer import PaymentDecision
from app.services.company_admission_auto import submit_or_admit
from app.services.company_admissions import fresh_user
from app.services.company_transfer_fence import fence_owned_agents
from app.services.company_transfer_guards import require_default_source
from app.services.notification_service import send_notification
from app.services.org_membership import get_fallback_org
from app.services.system_email_service import send_system_email


async def require_payment_reviewer(user_id: UUID) -> UserRecord:
    user = await fresh_user(user_id)
    if user.is_platform_admin:
        return user
    fallback = await get_fallback_org()
    if user.is_genesis and user.role == "org_admin" and user.tenant_id == fallback.id:
        return user
    raise HTTPException(403, "Payment review requires a platform admin or default-organization genesis admin")


async def admission_created_email(email: str, admission_id: UUID) -> None:
    await send_system_email(
        to=email,
        subject="Your company admission request has been created",
        body=f"Your payment has been confirmed. Admission request {admission_id} has been created "
        "and is awaiting approval by the target company's genesis administrator. You have not moved companies yet.",
    )


async def admission_approved_email(email: str, admission_id: UUID) -> None:
    await send_system_email(
        to=email,
        subject="Your company admission has been approved",
        body=f"Your payment has been confirmed and your invitation was accepted. Admission {admission_id} is "
        "approved and your account has moved to the new company.",
    )


async def decide_payment(request_id: UUID, reviewer_id: UUID, decision: PaymentDecision) -> CompanyTransferRecord:
    async with connection_ctx() as db:
        snapshot = await transfers.get(request_id)
        if snapshot is None:
            raise HTTPException(404, "Transfer request not found")
        applicant = await fresh_user(snapshot.applicant_user_id)
        reviewer = await require_payment_reviewer(reviewer_id)
        # An approved invitation admits immediately, so the agent fence precedes the tenant/user locks.
        fenced_ids = (
            await fence_owned_agents(applicant)
            if decision.decision == "approved"
            and snapshot.status == TransferStatus.PAYMENT_REVIEW
            and snapshot.method == AdmissionMethod.INVITATION
            else None
        )
        await lock_tenants(
            db, (snapshot.source_tenant_id, snapshot.target_tenant_id, applicant.tenant_id, reviewer.tenant_id)
        )
        for user in sorted((applicant, reviewer), key=lambda item: str(item.id)):
            await lock_user(db, user.id, user.tenant_id)
        await require_payment_reviewer(reviewer_id)
        applicant = await fresh_user(applicant.id)
        request = await transfers.get_locked(request_id)
        if request is None:
            raise HTTPException(404, "Transfer request not found")
        if request.choice != TransferChoice.PAID:
            raise HTTPException(409, "Free transfers do not require payment review")
        match request.status:
            case TransferStatus.ADMISSION_CREATED:
                if decision.decision == "approved":
                    return request
                raise HTTPException(409, "Transfer request already has a different decision")
            case TransferStatus.REJECTED:
                if decision.decision == "rejected":
                    return request
                raise HTTPException(409, "Transfer request already has a different decision")
            case TransferStatus.WITHDRAWN:
                raise HTTPException(409, "Transfer request was withdrawn")
            case TransferStatus.PAYMENT_REVIEW:
                pass
            case unreachable:
                assert_never(unreachable)
        if applicant.tenant_id != request.source_tenant_id:
            raise HTTPException(409, "Applicant membership changed")
        await require_default_source(applicant)
        now = datetime.now(UTC)
        reviewed = await transfers.update(
            db_obj=request,
            obj_in={"reviewed_at": now, "reviewed_by_user_id": reviewer_id, "review_reason": decision.reason},
        )
        match decision.decision:
            case "rejected":
                return await transfers.update(db_obj=reviewed, obj_in={"status": "rejected"})
            case "approved":
                confirmed = await transfers.update(db_obj=reviewed, obj_in={"payment_confirmed_at": now})
                admission = await submit_or_admit(
                    applicant,
                    request.target_tenant_id,
                    request.method,
                    invitation_code=request.invitation_code,
                    transfer_id=request.id,
                    fenced_ids=fenced_ids,
                )
                admitted = admission.status == AdmissionStatus.APPROVED
                result = await transfers.update(
                    db_obj=confirmed, obj_in={"status": "admission_created", "admission_request_id": admission.id}
                )
                await send_notification(
                    applicant.id,
                    notification_kind=NotificationKind.COMPANY_ADMISSION_RESOLVED
                    if admitted
                    else NotificationKind.COMPANY_ADMISSION_CREATED,
                    title="Company admission approved" if admitted else "Company admission request created",
                    body="Payment confirmed. You have joined the company."
                    if admitted
                    else "Payment confirmed. Your request is awaiting the target company's approval.",
                    link=f"/app/admissions/{admission.id}" if admitted else "/app/admissions",
                    ref_id=admission.id,
                )
                if applicant.email is not None and applicant.email != "":
                    schedule_background_task(
                        (admission_approved_email if admitted else admission_created_email)(
                            applicant.email, admission.id
                        ),
                        description=f"transfer admission created {admission.id}",
                    )
                return result
            case unreachable:
                assert_never(unreachable)


async def withdraw_transfer(request_id: UUID, applicant_id: UUID) -> CompanyTransferRecord:
    async with connection_ctx() as db:
        snapshot = await transfers.get(request_id)
        if snapshot is None:
            raise HTTPException(404, "Transfer request not found")
        if snapshot.applicant_user_id != applicant_id:
            raise HTTPException(403, "Only the applicant may withdraw a transfer")
        applicant = await fresh_user(applicant_id)
        await lock_tenants(db, (snapshot.source_tenant_id, snapshot.target_tenant_id, applicant.tenant_id))
        await lock_user(db, applicant.id, applicant.tenant_id)
        request = await transfers.get_locked(request_id)
        if request is None:
            raise HTTPException(404, "Transfer request not found")
        match request.status:
            case TransferStatus.WITHDRAWN:
                return request
            case TransferStatus.REJECTED:
                raise HTTPException(409, "Transfer request was rejected")
            case TransferStatus.ADMISSION_CREATED:
                raise HTTPException(409, "Withdraw the created admission request instead")
            case TransferStatus.PAYMENT_REVIEW:
                return await transfers.update(db_obj=request, obj_in={"status": "withdrawn"})
            case unreachable:
                assert_never(unreachable)
