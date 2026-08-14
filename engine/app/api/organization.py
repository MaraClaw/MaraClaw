"""Organization management API routes (users only)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import get_current_admin, get_current_user
from app.dao.identity_dao import identity_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord
from app.schemas.schemas import UserOut, UserUpdate

router = APIRouter(prefix="/org", tags=["organization"])


# ─── Users Management ──────────────────────────────────


@router.get("/users", response_model=list[UserOut])
async def list_users(
    tenant_id: uuid.UUID | None = None, current_user: UserRecord = Depends(get_current_user)
) -> list[UserOut]:
    """List users, optionally filtered by tenant."""
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
    return [UserOut.model_validate(u) for u in users]


@router.patch("/users/{user_id}", response_model=UserOut)
async def admin_update_user(
    user_id: uuid.UUID, data: UserUpdate, current_user: UserRecord = Depends(get_current_admin)
):
    """Admin update user profile."""
    _ = current_user
    user = await user_dao.get_with_identity(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = data.model_dump(exclude_unset=True)

    # Validate email uniqueness within tenant if changing
    if (
        "email" in update_data
        and update_data["email"] != user.email
        and user.tenant_id is not None
        and await identity_dao.is_email_taken_in_tenant(update_data["email"], user.tenant_id, exclude_user_id=user.id)
    ):
        raise HTTPException(status_code=409, detail="Email already registered")

    # Validate mobile uniqueness within tenant if changing
    if (
        "primary_mobile" in update_data
        and update_data["primary_mobile"] != user.primary_mobile
        and user.tenant_id is not None
        and await identity_dao.is_phone_taken_in_tenant(
            update_data["primary_mobile"], user.tenant_id, exclude_user_id=user.id
        )
    ):
        raise HTTPException(status_code=409, detail="Mobile already registered")

    user_fields = {}
    identity_fields = {}
    for field, value in update_data.items():
        if field in ("email", "username", "primary_mobile"):
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
    return UserOut.model_validate(refreshed or user)
