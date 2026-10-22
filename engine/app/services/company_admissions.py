from datetime import UTC, datetime
from typing import assert_never
from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_tenants, lock_user
from app.core.json_types import uuid_from_row
from app.dao.company_admission_dao import company_admission_dao as admissions
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod, AdmissionStatus, CompanyAdmissionRecord
from app.records.user import UserRecord
from app.schemas.company_admission import AdmissionApplicant, AdmissionTenant, RequestDetail, RequestSummary
from app.services.notification_service import send_notification


async def fresh_user(user_id: UUID, *, require_active: bool = True) -> UserRecord:
    user = await user_dao.get_with_identity(user_id, fresh=True)
    if (
        user is None
        or (require_active and not user.is_active)
        or (user.identity is not None and not user.identity.is_active)
    ):
        raise HTTPException(403, "Active membership required")
    if user.must_change_password:
        raise HTTPException(403, "Password change required")
    return user


async def require_genesis(user_id: UUID, target_id: UUID | None = None) -> UserRecord:
    user = await fresh_user(user_id)
    if not user.is_genesis or user.role != "org_admin" or user.is_platform_admin or user.tenant_id is None:
        raise HTTPException(403, "Only the company genesis administrator may review admissions")
    if target_id is not None and user.tenant_id != target_id:
        raise HTTPException(403, "Only the target company genesis administrator may review admissions")
    tenant = await admissions.tenant(user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(403, "Company is disabled")
    return user


async def summarize(request: CompanyAdmissionRecord) -> RequestSummary:
    tenant = await admissions.tenant(request.target_tenant_id)
    if tenant is None:
        raise HTTPException(404, "Company not found")
    return RequestSummary(
        id=request.id,
        status=request.status,
        source_tenant_id=request.source_tenant_id,
        target_tenant=AdmissionTenant(id=tenant.id, name=tenant.name),
        method=request.method,
        created_at=request.created_at,
        decided_at=request.decided_at,
        decision_reason=request.decision_reason,
    )


async def detail(request: CompanyAdmissionRecord) -> RequestDetail:
    summary = await summarize(request)
    applicant = await user_dao.get_with_identity(request.applicant_user_id, fresh=True)
    if applicant is None:
        raise HTTPException(404, "Applicant not found")
    source = await admissions.tenant(request.source_tenant_id) if request.source_tenant_id else None
    return RequestDetail(
        id=summary.id,
        status=summary.status,
        source_tenant_id=summary.source_tenant_id,
        target_tenant=summary.target_tenant,
        method=summary.method,
        created_at=summary.created_at,
        decided_at=summary.decided_at,
        decision_reason=summary.decision_reason,
        applicant=AdmissionApplicant(id=applicant.id, email=applicant.email),
        source_tenant=AdmissionTenant(id=source.id, name=source.name) if source else None,
        decided_by_user_id=request.decided_by_user_id,
    )


async def withdraw_request(request_id: UUID, applicant_id: UUID) -> CompanyAdmissionRecord:
    async with connection_ctx() as db:
        snapshot = await admissions.get(request_id)
        if snapshot is None:
            raise HTTPException(404, "Admission request not found")
        if snapshot.applicant_user_id != applicant_id:
            raise HTTPException(403, "Only the applicant may withdraw an admission request")
        applicant = await user_dao.get(applicant_id)
        if applicant is None:
            raise HTTPException(404, "Membership not found")
        await lock_tenants(db, (snapshot.source_tenant_id, snapshot.target_tenant_id, applicant.tenant_id))
        await lock_user(db, applicant.id, applicant.tenant_id)
        await fresh_user(applicant_id)
        request = await admissions.get_locked(request_id)
        if request is None:
            raise HTTPException(404, "Admission request not found")
        match request.status:
            case AdmissionStatus.WITHDRAWN:
                return request
            case AdmissionStatus.APPROVED | AdmissionStatus.REJECTED:
                raise HTTPException(409, "Admission request already has a different decision")
            case AdmissionStatus.PENDING:
                return await admissions.update(
                    db_obj=request,
                    obj_in={
                        "status": AdmissionStatus.WITHDRAWN.value,
                        "decided_at": datetime.now(UTC),
                        "decided_by_user_id": None,
                        "decision_reason": None,
                    },
                )
            case unreachable:
                assert_never(unreachable)


async def submit_request(
    applicant: UserRecord,
    target_id: UUID,
    method: AdmissionMethod,
    *,
    invitation_code: str | None = None,
    newly_registered: bool = False,
) -> CompanyAdmissionRecord:
    async with connection_ctx() as db:
        await lock_tenants(db, (applicant.tenant_id, target_id))
        await lock_user(db, applicant.id, applicant.tenant_id)
        current = await fresh_user(applicant.id, require_active=not newly_registered)
        if current.is_genesis or current.is_platform_admin or current.role == "org_admin":
            raise HTTPException(403, "Admin membership cannot be converted")
        target = await admissions.tenant(target_id)
        if target is None or not target.is_active or target.is_system or target.is_default_end_user_org:
            raise HTTPException(400, "Target must be an active company")
        if current.tenant_id == target_id:
            raise HTTPException(409, "You already belong to that company")
        pending = await admissions.pending_for(current.id)
        if pending is not None:
            if pending.target_tenant_id != target_id:
                raise HTTPException(409, "An admission request for another company is pending")
            return pending
        row = await db.fetchone(
            "SELECT id FROM users WHERE tenant_id=%(id)s AND role='org_admin' AND is_genesis AND is_active",
            {"id": target_id},
        )
        if row is None:
            raise HTTPException(409, "Company has no active genesis administrator")
        reviewer_id = uuid_from_row(row["id"])
        await lock_user(db, reviewer_id, target_id)
        await require_genesis(reviewer_id, target_id)
        await validate_method(current, target_id, method, invitation_code)
        request = await admissions.create(
            obj_in={
                "applicant_user_id": current.id,
                "source_tenant_id": current.tenant_id,
                "target_tenant_id": target_id,
                "method": method.value,
                "invitation_code": invitation_code,
            }
        )
        await send_notification(
            reviewer_id,
            type="company_admission_pending",
            title="Company admission requested",
            body=current.display_name,
            link=f"/admission-approvals/{request.id}",
            ref_id=request.id,
        )
        return request


async def validate_method(user: UserRecord, target_id: UUID, method: AdmissionMethod, code: str | None) -> None:
    match method:
        case AdmissionMethod.INVITATION:
            async with connection_ctx() as db:
                invitation = await db.fetchone(
                    "SELECT tenant_id FROM invitation_codes WHERE code=%(code)s AND is_active "
                    "AND used_count < max_uses FOR UPDATE",
                    {"code": code},
                )
                if invitation is None or invitation["tenant_id"] != target_id:
                    raise HTTPException(409, "Invitation is no longer valid")
        case AdmissionMethod.VERIFIED_DOMAIN:
            from app.services.org_membership import lookup_tenant_for_verified_email

            target = await lookup_tenant_for_verified_email(user)
            if target is None or target.id != target_id:
                raise HTTPException(403, "Verified email domain does not match the company")
        case AdmissionMethod.SSO:
            pass
        case unreachable:
            assert_never(unreachable)
