"""Organization management API routes (users only)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.core.json_types import json_as_str, object_mapping_from
from app.core.security import get_current_admin, get_current_user
from app.dao.identity_dao import identity_dao
from app.dao.user_dao import user_dao
from app.db.session import optional_connection_ctx
from app.records.user import UserRecord
from app.schemas.schemas import AuthUser, UserUpdate
from app.services.admin_audit import field_change, write_admin_audit

router = APIRouter(prefix="/org", tags=["organization"])


# ─── Users Management ──────────────────────────────────


@router.get("/users", response_model=list[AuthUser])
async def list_users(
    tenant_id: uuid.UUID | None = None, current_user: UserRecord = Depends(get_current_user)
) -> list[AuthUser]:
    """List users, optionally filtered by tenant."""
    if (
        current_user.role == "org_admin"
        and not current_user.is_platform_admin
        and tenant_id is not None
        and tenant_id != current_user.tenant_id
    ):
        raise HTTPException(status_code=403, detail="Cannot access other tenant's users")
    target_tenant_id = current_user.tenant_id
    if current_user.role in ("platform_admin", "org_admin") and tenant_id:
        target_tenant_id = tenant_id
    if not target_tenant_id:
        return []

    users = await user_dao.list_active_for_tenant(
        target_tenant_id,
        include_identity=True,
        order_by_display_name=True,
    )
    return [AuthUser.model_validate(u) for u in users]


@router.patch("/users/{user_id}", response_model=AuthUser)
async def admin_update_user(
    user_id: uuid.UUID, data: UserUpdate, current_user: UserRecord = Depends(get_current_admin)
):
    """Admin update user profile."""
    async with optional_connection_ctx():
        user = await user_dao.get_with_identity(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        update_data = object_mapping_from(data.model_dump(exclude_unset=True))
        if not current_user.is_platform_admin:
            if current_user.tenant_id is None or user.tenant_id != current_user.tenant_id:
                raise HTTPException(status_code=404, detail="User not found")
            if user.is_platform_admin:
                raise HTTPException(status_code=403, detail="Cannot edit a platform admin")
            if user.is_genesis and {"email", "primary_mobile"}.intersection(update_data):
                raise HTTPException(status_code=403, detail="Cannot edit a genesis user's identity")
        email = json_as_str(update_data.get("email"))
        primary_mobile = json_as_str(update_data.get("primary_mobile"))

        # Validate email uniqueness within tenant if changing
        if (
            email is not None
            and email != user.email
            and user.tenant_id is not None
            and await identity_dao.is_email_taken_in_tenant(email, user.tenant_id, exclude_user_id=user.id)
        ):
            raise HTTPException(status_code=409, detail="Email already registered")

        # Validate mobile uniqueness within tenant if changing
        if (
            primary_mobile is not None
            and primary_mobile != user.primary_mobile
            and user.tenant_id is not None
            and await identity_dao.is_phone_taken_in_tenant(primary_mobile, user.tenant_id, exclude_user_id=user.id)
        ):
            raise HTTPException(status_code=409, detail="Mobile already registered")

        before = {
            "display_name": user.display_name,
            "title": user.title,
            "email": user.email,
            "primary_mobile": user.primary_mobile,
        }
        before_avatar = user.avatar_url
        user_fields: dict[str, object] = {}
        identity_fields: dict[str, object] = {}
        for field, value in update_data.items():
            if field in ("email", "primary_mobile"):
                identity_fields["phone" if field == "primary_mobile" else field] = value
            else:
                user_fields[field] = value

        if user_fields:
            user = await user_dao.update(db_obj=user, obj_in=user_fields) or user
        if identity_fields and user.identity_id:
            identity = await identity_dao.get(user.identity_id)
            if identity:
                _ = await identity_dao.update(db_obj=identity, obj_in=identity_fields)

        # Sync email/phone to OrgMember if changed
        if "email" in update_data or "primary_mobile" in update_data:
            from app.services.registration_service import registration_service

            user = await user_dao.get_with_identity(user_id) or user
            await registration_service.sync_org_member_contact_from_user(
                user,
                sync_email="email" in update_data,
                sync_phone="primary_mobile" in update_data,
            )

        refreshed = await user_dao.get_with_identity(user_id)
        result = refreshed or user
        after = {
            "display_name": result.display_name,
            "title": result.title,
            "email": result.email,
            "primary_mobile": result.primary_mobile,
        }
        changes = {field: field_change(value, after[field]) for field, value in before.items() if value != after[field]}
        avatar_changed = before_avatar != result.avatar_url
        if changes or avatar_changed:
            await write_admin_audit(
                actor=current_user,
                action="user.profile_updated",
                target_type="user",
                target_id=result.id,
                tenant_id=result.tenant_id,
                changes=changes,
                # Avatar URLs can contain credentials; record only the change indicator.
                details={"avatar_changed": avatar_changed},
            )
        return AuthUser.model_validate(result)
