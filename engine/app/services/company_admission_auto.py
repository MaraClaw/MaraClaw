from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_tenants, lock_user
from app.dao.company_admission_dao import company_admission_dao as admissions
from app.dao.company_transfer_dao import company_transfer_dao as transfers
from app.dao.invitation_code_dao import invitation_code_dao
from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod, AdmissionStatus, CompanyAdmissionRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.admin_provisioning import AdminGuardError, assert_join_may_rewrite_membership
from app.services.company_admission_apply import apply_approved_admission
from app.services.company_admissions import fresh_user, submit_request
from app.services.company_transfer_fence import fence_owned_agents
from app.services.company_transfer_guards import require_admission_transfer
from app.services.invitation_codes import normalize_invite_email
from app.services.org_welcome import send_organization_welcome

INVITATION_UNAVAILABLE = "Invitation is invalid or no longer available"


async def preview_invitation(user: UserRecord, code: str) -> TenantRecord:
    """Destination of a code for an eligible default/tenantless member; one generic failure for everything else."""
    unavailable = HTTPException(400, INVITATION_UNAVAILABLE)
    try:
        assert_join_may_rewrite_membership(user)
    except AdminGuardError as exc:
        raise unavailable from exc
    if user.tenant_id is not None:
        source = await admissions.tenant(user.tenant_id)
        if source is None or not source.is_active or not source.is_default_end_user_org:
            raise unavailable
    invitation = await invitation_code_dao.get_active_by_code(code, email=normalize_invite_email(user.email))
    if invitation is None or invitation.used_count >= invitation.max_uses or invitation.tenant_id is None:
        raise unavailable
    target = await admissions.tenant(invitation.tenant_id)
    if target is None or not target.is_active or target.is_system or target.is_default_end_user_org:
        raise unavailable
    if target.id == user.tenant_id:
        raise unavailable
    return target


async def admit_with_invitation(
    applicant: UserRecord,
    target_id: UUID,
    code: str,
    *,
    transfer_id: UUID | None = None,
    fenced_ids: frozenset[UUID] | None = None,
) -> CompanyAdmissionRecord:
    """Create an already-approved admission and move the member in one transaction (no reviewer involved).

    Callers that already hold tenant/user locks must pass ``fenced_ids`` taken before those locks.
    """
    async with connection_ctx() as db:
        if fenced_ids is None:
            fenced_ids = await fence_owned_agents(applicant)
        await lock_tenants(db, (applicant.tenant_id, target_id))
        await lock_user(db, applicant.id, applicant.tenant_id)
        current = await fresh_user(applicant.id)
        if current.is_genesis or current.is_platform_admin or current.role == "org_admin":
            raise HTTPException(403, "Admin membership cannot be converted")
        target = await admissions.tenant(target_id)
        if target is None or not target.is_active or target.is_system or target.is_default_end_user_org:
            raise HTTPException(400, "Target must be an active company")
        if current.tenant_id == target_id:
            raise HTTPException(409, "You already belong to that company")
        await require_admission_transfer(current, target_id, transfer_id)
        request = await admissions.pending_for(current.id)
        if request is not None and request.target_tenant_id != target_id:
            raise HTTPException(409, "An admission request for another company is pending")
        values = {"method": AdmissionMethod.INVITATION.value, "invitation_code": code}
        if request is None:
            request = await admissions.create(
                obj_in={
                    "applicant_user_id": current.id,
                    "source_tenant_id": current.tenant_id,
                    "target_tenant_id": target_id,
                    **values,
                }
            )
        else:
            request = await admissions.update(db_obj=request, obj_in=values)
        if transfer_id is not None:
            transfer = await transfers.get_locked(transfer_id)
            if transfer is not None:
                _ = await transfers.update(
                    db_obj=transfer, obj_in={"status": "admission_created", "admission_request_id": request.id}
                )
        welcome = await apply_approved_admission(db, request, fenced_ids)
        approved = await admissions.update(
            db_obj=request,
            obj_in={
                "status": AdmissionStatus.APPROVED.value,
                "decided_at": datetime.now(UTC),
                "decided_by_user_id": None,
                "decision_reason": None,
            },
        )
        _ = await send_organization_welcome(*welcome)
        return approved


async def submit_or_admit(
    applicant: UserRecord,
    target_id: UUID,
    method: AdmissionMethod,
    *,
    invitation_code: str | None = None,
    transfer_id: UUID | None = None,
    fenced_ids: frozenset[UUID] | None = None,
) -> CompanyAdmissionRecord:
    """Invitations admit immediately; domain and SSO requests still wait for the genesis administrator."""
    if method == AdmissionMethod.INVITATION and invitation_code is not None:
        return await admit_with_invitation(
            applicant, target_id, invitation_code, transfer_id=transfer_id, fenced_ids=fenced_ids
        )
    return await submit_request(applicant, target_id, method, invitation_code=invitation_code, transfer_id=transfer_id)
