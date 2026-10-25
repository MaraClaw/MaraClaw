from typing import TypeGuard

from app.core.acl_locks import lock_tenants, lock_user
from app.core.json_types import uuid_from_row
from app.dao.company_admission_dao import company_admission_dao as admissions
from app.dao.company_transfer_dao import company_transfer_dao as transfers
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod, CompanyAdmissionRecord
from app.records.user import UserRecord
from app.services.company_admissions import submit_request
from app.services.company_transfer_guards import has_owned_agents
from app.services.org_membership import lookup_tenant_for_verified_email


def eligible_member(user: UserRecord | None) -> TypeGuard[UserRecord]:
    return (
        user is not None
        and user.is_active
        and user.email_verified
        and not user.is_genesis
        and not user.is_platform_admin
        and user.role == "member"
        and not user.must_change_password
        and user.identity is not None
        and user.identity.is_active
    )


async def stage_verified_domain_admission(user: UserRecord) -> CompanyAdmissionRecord | None:
    if user.tenant_id is None:
        return None
    current = await user_dao.get_with_identity(user.id, fresh=True)
    if not eligible_member(current):
        return None
    target = await lookup_tenant_for_verified_email(current)
    if target is None or target.is_system or target.is_default_end_user_org:
        return None
    async with connection_ctx() as db:
        await lock_tenants(db, (user.tenant_id, target.id))
        await lock_user(db, user.id, user.tenant_id)
        current = await user_dao.get_with_identity(user.id, fresh=True)
        if not eligible_member(current):
            return None
        source = await admissions.tenant(user.tenant_id)
        if source is None or not source.is_active or not source.is_default_end_user_org:
            return None
        if await admissions.pending_for(user.id) is not None:
            return None
        if await admissions.has_invitation_history(user.id):
            return None
        if await transfers.active_for(user.id) is not None or await has_owned_agents(current):
            return None
        row = await db.fetchone(
            "SELECT id FROM users WHERE tenant_id=%(id)s AND role='org_admin' AND is_genesis AND is_active",
            {"id": target.id},
        )
        if row is None:
            return None
        reviewer_id = uuid_from_row(row["id"])
        await lock_user(db, reviewer_id, target.id)
        reviewer = await user_dao.get_with_identity(reviewer_id, fresh=True)
        if (
            reviewer is None
            or not reviewer.is_active
            or not reviewer.is_genesis
            or reviewer.role != "org_admin"
            or reviewer.is_platform_admin
            or reviewer.must_change_password
            or (reviewer.identity is not None and not reviewer.identity.is_active)
        ):
            return None
        return await submit_request(current, target.id, AdmissionMethod.VERIFIED_DOMAIN)
