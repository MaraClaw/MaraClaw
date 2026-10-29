from uuid import UUID

from app.core.json_types import uuid_from_row
from app.dao.company_admission_dao import company_admission_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod
from app.records.identity import IdentityProviderRecord
from app.records.user import UserRecord
from app.services.company_admissions import fresh_user, submit_request


async def find_pending_sso_user(provider: IdentityProviderRecord, external_id: str) -> UserRecord | None:
    async with connection_ctx() as db:
        row = await db.fetchone(
            "SELECT m.user_id FROM org_members m JOIN users u ON u.id=m.user_id "
            "WHERE m.provider_id=%(provider)s AND (m.external_id=%(external)s OR m.unionid=%(external)s OR m.open_id=%(external)s) "
            "AND m.tenant_id IS NOT DISTINCT FROM u.tenant_id ORDER BY m.id LIMIT 1",
            {"provider": provider.id, "external": external_id},
        )
        return await fresh_user(uuid_from_row(row["user_id"])) if row is not None else None


async def stage_sso_admission(
    user: UserRecord, provider: IdentityProviderRecord, external_id: str, *, target_id: UUID | None
) -> bool:
    linked = await find_pending_sso_user(provider, external_id)
    if (
        user.tenant_id is not None
        and linked is not None
        and linked.id == user.id
        and await company_admission_dao.has_approved_initial_domain_admission(user.id, user.tenant_id)
    ):
        return True
    if user.tenant_id is None:
        from app.services.domain_admission_auto import stage_verified_domain_admission

        await stage_verified_domain_admission(user)
    else:
        if target_id is None or user.tenant_id == target_id:
            return False
        target = await company_admission_dao.tenant(target_id)
        if target is None or target.is_system or target.is_default_end_user_org:
            return False
        await submit_request(user, target_id, AdmissionMethod.SSO)
    if linked is None:
        await org_member_dao.create(
            obj_in={
                "provider_id": provider.id,
                "user_id": user.id,
                "tenant_id": user.tenant_id,
                "external_id": external_id,
                "name": user.display_name,
            }
        )
    return True


async def create_sso_user(identity_id: UUID, display_name: str, *, source: str, avatar: str | None) -> UserRecord:
    from app.dao.identity_dao import identity_dao
    from app.services.org_membership import place_new_registration
    from app.services.registration_service import registration_service

    existing = await user_dao.get_by_identity_id(identity_id)
    if existing:
        return await fresh_user(existing[0].id)
    identity = await identity_dao.get(identity_id)
    if identity is None:
        raise ValueError("Identity not found")
    placement = await place_new_registration(email=identity.email)
    user = await registration_service.create_user_with_identity(
        identity=identity,
        display_name=display_name,
        tenant_id=placement.tenant_id,
    )
    updated = await user_dao.update(
        db_obj=user, obj_in={"registration_source": source, "avatar_url": avatar, "is_active": True}
    )
    updated.identity = identity
    return updated
