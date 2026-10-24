from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_tenants, lock_user
from app.dao.company_admission_dao import company_admission_dao as admissions
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod, AdmissionStatus, CompanyAdmissionRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.company_admission import AdmissionDecision
from app.services.admin_audit import field_change, write_admin_audit
from app.services.company_admissions import fresh_user, require_genesis, validate_method
from app.services.company_transfer_disposition import apply_disposition
from app.services.company_transfer_fence import fence_owned_agents
from app.services.notification_service import send_notification
from app.services.org_membership import AlreadyInOrgError, DefaultOrgUnavailableError, transfer_user_to_org
from app.services.org_welcome import send_organization_welcome


async def decide(request_id: UUID, reviewer_id: UUID, decision: AdmissionDecision) -> CompanyAdmissionRecord:
    async with connection_ctx() as db:
        snapshot = await admissions.get(request_id)
        if snapshot is None:
            raise HTTPException(404, "Admission request not found")
        applicant = await user_dao.get(snapshot.applicant_user_id)
        reviewer = await user_dao.get(reviewer_id)
        if applicant is None or reviewer is None:
            raise HTTPException(404, "Membership not found")
        reviewer = await require_genesis(reviewer_id, snapshot.target_tenant_id)
        fenced_ids = frozenset[UUID]()
        if snapshot.status == AdmissionStatus.PENDING and decision.decision == "approved":
            fenced_ids = await fence_owned_agents(applicant)
        await lock_tenants(
            db, (snapshot.source_tenant_id, snapshot.target_tenant_id, applicant.tenant_id, reviewer.tenant_id)
        )
        for user in sorted((applicant, reviewer), key=lambda member: member.id):
            await lock_user(db, user.id, user.tenant_id)
        reviewer = await require_genesis(reviewer_id, snapshot.target_tenant_id)
        request = await admissions.get_locked(request_id)
        if request is None:
            raise HTTPException(404, "Admission request not found")
        result = AdmissionStatus(decision.decision)
        if request.status != AdmissionStatus.PENDING:
            if request.status != result:
                raise HTTPException(409, "Admission request already has a different decision")
            return request
        welcome_context: tuple[UserRecord, TenantRecord] | None = None
        if result == AdmissionStatus.APPROVED:
            current = await fresh_user(request.applicant_user_id)
            if current.tenant_id != request.source_tenant_id:
                raise HTTPException(409, "Applicant organization changed since the request")
            if current.is_genesis or current.role in {"org_admin", "platform_admin"} or current.is_platform_admin:
                raise HTTPException(403, "Admin membership cannot be transferred")
            target = await admissions.tenant(request.target_tenant_id)
            if target is None or not target.is_active:
                raise HTTPException(409, "Company is disabled")
            await validate_method(current, target.id, request.method, request.invitation_code)
            await apply_disposition(request, current, fenced_ids)
            await db.execute("DELETE FROM local_department_memberships WHERE user_id=%(user)s", {"user": current.id})
            sso_members = await db.fetchall(
                "SELECT m.id FROM org_members m JOIN identity_providers p ON p.id=m.provider_id "
                "WHERE m.user_id=%(user)s AND (p.tenant_id=%(target)s OR p.tenant_id IS NULL) "
                "AND p.provider_type <> 'web'",
                {"user": current.id, "target": target.id},
            )
            try:
                await transfer_user_to_org(current, target)
            except (AlreadyInOrgError, DefaultOrgUnavailableError) as exc:
                raise HTTPException(409, str(exc)) from exc
            for member in sso_members:
                await db.execute(
                    "UPDATE org_members SET tenant_id=%(tenant)s,user_id=%(user)s WHERE id=%(id)s",
                    {"tenant": target.id, "user": current.id, "id": member["id"]},
                )
            if request.method == AdmissionMethod.INVITATION:
                await db.execute(
                    "UPDATE invitation_codes SET used_count=used_count+1 WHERE code=%(code)s",
                    {"code": request.invitation_code},
                )
            welcome_context = (current, target)
        before_status = request.status.value
        resolved = await admissions.update(
            db_obj=request,
            obj_in={
                "status": result.value,
                "decided_at": datetime.now(UTC),
                "decided_by_user_id": reviewer_id,
                "decision_reason": decision.reason,
            },
        )
        if welcome_context is not None:
            await send_organization_welcome(*welcome_context)
        await write_admin_audit(
            actor=reviewer,
            action="company_admission.decided",
            target_type="company_admission",
            target_id=resolved.id,
            tenant_id=resolved.target_tenant_id,
            changes={"status": field_change(before_status, resolved.status.value)},
            details={
                "applicant_user_id": str(resolved.applicant_user_id),
                "source_tenant_id": str(resolved.source_tenant_id),
                "method": resolved.method.value,
            },
        )
        await send_notification(
            request.applicant_user_id,
            type="company_admission_resolved",
            title=f"Company admission {result.value}",
            body=decision.reason or "",
            link=f"/app/admissions/{request.id}",
            ref_id=request.id,
        )
        return resolved
