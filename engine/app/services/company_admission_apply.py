from uuid import UUID

from fastapi import HTTPException

from app.dao.company_admission_dao import company_admission_dao as admissions
from app.dao.invitation_code_dao import invitation_code_dao
from app.db.connection import DbConnection
from app.records.company_admission import AdmissionMethod, CompanyAdmissionRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.company_admissions import fresh_user, validate_method
from app.services.company_transfer_disposition import apply_disposition
from app.services.invitation_codes import normalize_invite_email
from app.services.org_membership import AlreadyInOrgError, DefaultOrgUnavailableError, transfer_user_to_org


async def apply_approved_admission(
    db: DbConnection, request: CompanyAdmissionRecord, fenced_ids: frozenset[UUID]
) -> tuple[UserRecord, TenantRecord]:
    """Revalidate and execute an approved admission: disposition, membership move, invitation spend.

    The caller owns the transaction, tenant/user locks and agent fencing, and records the decision.
    """
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
        _ = await transfer_user_to_org(current, target)
    except (AlreadyInOrgError, DefaultOrgUnavailableError) as exc:
        raise HTTPException(409, str(exc)) from exc
    for member in sso_members:
        await db.execute(
            "UPDATE org_members SET tenant_id=%(tenant)s,user_id=%(user)s WHERE id=%(id)s",
            {"tenant": target.id, "user": current.id, "id": member["id"]},
        )
    if request.method == AdmissionMethod.INVITATION and request.invitation_code is not None:
        spent = await invitation_code_dao.consume(
            request.invitation_code, user_id=current.id, email=normalize_invite_email(current.email)
        )
        if not spent:
            raise HTTPException(409, "Invitation is no longer valid")
    return current, target
