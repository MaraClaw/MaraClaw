"""Create additional platform / org admins. Only genesis admins may do this."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.security import hash_password_async
from app.dao.identity_dao import identity_dao
from app.dao.participant_dao import participant_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.errors import UniqueViolationError
from app.db.session import connection_ctx
from app.records.user import UserRecord
from app.services.admin_audit import field_change, write_admin_audit
from app.services.tenant_provisioning import AdminEmailTakenError


@dataclass(slots=True, frozen=True)
class ProvisionedAdmin:
    user: UserRecord
    admin_email: str


def _flag(user: UserRecord, name: str, default: bool = False) -> bool:
    return bool(getattr(user, name, default))


async def is_genesis_platform_admin(user: UserRecord) -> bool:
    """True when ``user`` holds the persisted genesis platform-admin flag."""
    return getattr(user, "role", None) == "platform_admin" and _flag(user, "is_genesis")


async def is_genesis_org_admin(user: UserRecord) -> bool:
    """True when ``user`` holds the persisted genesis org-admin flag for their tenant."""
    return (
        getattr(user, "role", None) == "org_admin"
        and getattr(user, "tenant_id", None) is not None
        and _flag(user, "is_genesis")
    )


def _username_from_email(email: str, *, fallback: str) -> str:
    return email.split("@", 1)[0][:100] or fallback


async def _unique_username(email: str, *, fallback: str) -> str:
    base = _username_from_email(email, fallback=fallback)
    if not await identity_dao.is_username_taken(base):
        return base
    return f"{base}_{secrets.token_hex(3)}"[:100]


async def create_additional_platform_admin(
    *,
    admin_email: str,
    admin_password: str,
    admin_display_name: str | None = None,
) -> ProvisionedAdmin:
    """Provision a new null-tenant platform admin (must change password)."""
    email = admin_email.strip().lower()
    if await identity_dao.get_by_email(email):
        raise AdminEmailTakenError(email)

    password_hash = await hash_password_async(admin_password)
    username = await _unique_username(email, fallback="platform-admin")
    display_name = (admin_display_name or "").strip() or username

    try:
        async with connection_ctx():
            identity = await identity_dao.create_identity(
                email=email,
                username=username,
                password_hash=password_hash,
                is_platform_admin=True,
                email_verified=True,
                must_change_password=True,
            )
            user = await user_dao.create(
                obj_in={
                    "identity_id": identity.id,
                    "tenant_id": None,
                    "display_name": display_name,
                    "role": "platform_admin",
                    "registration_source": "platform_admin",
                    "is_active": True,
                    "is_genesis": False,
                }
            )
            user.identity = identity
            _ = await participant_dao.create_for_user(
                user.id,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
            )
    except UniqueViolationError as exc:
        raise AdminEmailTakenError(email) from exc

    return ProvisionedAdmin(user=user, admin_email=email)


async def create_additional_org_admin(
    *,
    tenant_id: UUID,
    admin_email: str,
    admin_password: str,
    admin_display_name: str | None = None,
) -> ProvisionedAdmin:
    """Provision a new org admin in ``tenant_id`` (must change password)."""
    email = admin_email.strip().lower()
    if await identity_dao.get_by_email(email):
        raise AdminEmailTakenError(email)

    tenant = await tenant_dao.get(tenant_id)
    if tenant is None:
        raise ValueError("tenant not found")

    password_hash = await hash_password_async(admin_password)
    username = await _unique_username(email, fallback="org-admin")
    display_name = (admin_display_name or "").strip() or username

    try:
        async with connection_ctx():
            identity = await identity_dao.create_identity(
                email=email,
                username=username,
                password_hash=password_hash,
                is_platform_admin=False,
                email_verified=True,
                must_change_password=True,
            )
            user = await user_dao.create(
                obj_in={
                    "identity_id": identity.id,
                    "tenant_id": tenant.id,
                    "display_name": display_name,
                    "role": "org_admin",
                    "registration_source": "org_admin",
                    "is_active": True,
                    "is_genesis": False,
                    "quota_message_limit": tenant.default_message_limit,
                    "quota_message_period": tenant.default_message_period,
                    "quota_max_agents": tenant.default_max_agents,
                    "quota_agent_ttl_hours": tenant.default_agent_ttl_hours,
                }
            )
            user.identity = identity
            _ = await participant_dao.create_for_user(
                user.id,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
            )
            from app.services.registration_service import registration_service

            await registration_service.bind_org_member(user)
    except UniqueViolationError as exc:
        raise AdminEmailTakenError(email) from exc

    return ProvisionedAdmin(user=user, admin_email=email)


class AdminGuardError(Exception):
    """An admin membership mutation is not allowed."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code: int = status_code
        self.detail: str = detail


AdminActivationError = AdminGuardError


def assert_join_may_rewrite_membership(user: UserRecord) -> None:
    """Refuse converting a genesis or platform-admin row into a company join."""
    if _flag(user, "is_genesis"):
        raise AdminGuardError(403, "Genesis membership cannot be converted by joining a company")
    if getattr(user, "role", None) == "platform_admin":
        raise AdminGuardError(403, "Platform admin membership cannot be converted by joining a company")


async def _assert_not_last_active_admin(target: UserRecord, *, leaving_role: str) -> None:
    if not getattr(target, "is_active", True):
        return
    if leaving_role == "platform_admin":
        count = await user_dao.count_active_by_role("platform_admin")
        if count <= 1:
            raise AdminGuardError(400, "Cannot change the only active platform administrator. Create another first.")
    elif leaving_role == "org_admin":
        count = await user_dao.count_active_by_role("org_admin", tenant_id=target.tenant_id)
        if count <= 1:
            raise AdminGuardError(
                400, "Cannot change the only active organization administrator. Promote another first."
            )


async def _sync_platform_admin_flag(target: UserRecord, *, is_platform_admin: bool) -> None:
    if target.identity is not None:
        _ = await identity_dao.update(db_obj=target.identity, obj_in={"is_platform_admin": is_platform_admin})
        target.identity.is_platform_admin = is_platform_admin


async def apply_user_role_change(
    *,
    actor: UserRecord,
    target: UserRecord,
    new_role: str,
    ip_address: str | None = None,
) -> UserRecord:
    """Change a user's role using the same genesis / last-active rules as mint and deactivate."""
    if getattr(actor, "role", None) not in ("platform_admin", "org_admin"):
        raise AdminGuardError(403, "Admin access required")
    if new_role not in ("platform_admin", "org_admin", "member"):
        raise AdminGuardError(400, "Invalid role. Allowed: platform_admin, org_admin, member")
    if actor.role == "org_admin" and target.tenant_id != actor.tenant_id:
        raise AdminGuardError(403, "Cannot modify users outside your organization")
    if target.role == new_role:
        return target

    if _flag(target, "is_genesis"):
        raise AdminGuardError(403, "Cannot change the genesis admin's role")

    if new_role == "platform_admin":
        if not await is_genesis_platform_admin(actor):
            raise AdminGuardError(403, "Only the genesis platform admin can create platform admins")
    elif new_role == "org_admin":
        if actor.role != "org_admin" or not await is_genesis_org_admin(actor):
            raise AdminGuardError(403, "Only the genesis organization admin can create org admins")
        if target.tenant_id != actor.tenant_id:
            raise AdminGuardError(403, "Cannot modify users outside your organization")

    if target.role == "platform_admin" and new_role != "platform_admin":
        await _assert_not_last_active_admin(target, leaving_role="platform_admin")
    elif target.role == "org_admin" and new_role != "org_admin":
        await _assert_not_last_active_admin(target, leaving_role="org_admin")

    user_updates: dict[str, object] = {"role": new_role, "is_genesis": False}
    async with connection_ctx():
        if new_role == "platform_admin":
            user_updates["tenant_id"] = None
            await _sync_platform_admin_flag(target, is_platform_admin=True)
        elif target.role == "platform_admin":
            await _sync_platform_admin_flag(target, is_platform_admin=False)
        updated = await user_dao.update(db_obj=target, obj_in=user_updates) or target

    await write_admin_audit(
        actor=actor,
        action="role_assign",
        target_type="user",
        target_id=target.id,
        tenant_id=target.tenant_id if new_role != "platform_admin" else None,
        changes={"role": field_change(target.role, new_role)},
        details={"target_email": getattr(target, "email", None)},
        ip_address=ip_address,
    )
    return updated


async def apply_user_assignment(
    *,
    actor: UserRecord,
    target: UserRecord,
    tenant_id: UUID,
    role: str,
    ip_address: str | None = None,
) -> UserRecord:
    """Move a user into a tenant. Cannot touch genesis or last-active admins."""
    if role not in ("agent_admin", "member"):
        raise AdminGuardError(400, "Invalid role")
    if _flag(target, "is_genesis"):
        raise AdminGuardError(403, "Cannot reassign a genesis admin")
    if target.role == "platform_admin":
        await _assert_not_last_active_admin(target, leaving_role="platform_admin")
    elif target.role == "org_admin":
        await _assert_not_last_active_admin(target, leaving_role="org_admin")

    previous_role = target.role
    previous_tenant = target.tenant_id
    async with connection_ctx():
        if target.role == "platform_admin":
            await _sync_platform_admin_flag(target, is_platform_admin=False)
        updated = (
            await user_dao.update(
                db_obj=target,
                obj_in={"tenant_id": tenant_id, "role": role, "is_genesis": False},
            )
            or target
        )

    await write_admin_audit(
        actor=actor,
        action="user_assign",
        target_type="user",
        target_id=target.id,
        tenant_id=tenant_id,
        changes={
            "role": field_change(previous_role, role),
            "tenant_id": field_change(str(previous_tenant) if previous_tenant else None, str(tenant_id)),
        },
        details={"target_email": getattr(target, "email", None)},
        ip_address=ip_address,
    )
    return updated


async def set_peer_admin_active(
    *,
    actor: UserRecord,
    target: UserRecord,
    is_active: bool,
    ip_address: str | None = None,
) -> UserRecord:
    """Genesis admin activates or deactivates another admin of the same role."""
    if target.id == actor.id:
        raise AdminActivationError(400, "Cannot change your own active status")

    tenant_for_count: UUID | None = None
    if actor.role == "platform_admin":
        if not await is_genesis_platform_admin(actor):
            raise AdminActivationError(
                403, "Only the genesis platform admin can activate or deactivate platform admins"
            )
        if target.role != "platform_admin":
            raise AdminActivationError(400, "Target must be a platform admin")
        if await is_genesis_platform_admin(target):
            raise AdminActivationError(403, "Cannot deactivate the genesis platform admin")
    elif actor.role == "org_admin":
        if not await is_genesis_org_admin(actor):
            raise AdminActivationError(403, "Only the genesis organization admin can activate or deactivate org admins")
        if target.role != "org_admin" or target.tenant_id != actor.tenant_id:
            raise AdminActivationError(403, "Can only change org admins in your own company")
        if await is_genesis_org_admin(target):
            raise AdminActivationError(403, "Cannot deactivate the genesis organization admin")
        tenant_for_count = actor.tenant_id
    else:
        raise AdminActivationError(403, "Admin access required")

    if target.is_active == is_active:
        return target

    async with connection_ctx():
        if not is_active and target.is_active:
            updated = await user_dao.deactivate_unless_last_active(
                target.id, role=target.role, tenant_id=tenant_for_count
            )
            if updated is None:
                raise AdminActivationError(
                    400,
                    "Cannot deactivate the only active platform admin"
                    if target.role == "platform_admin"
                    else "Cannot deactivate the only active organization admin",
                )
        else:
            updated = await user_dao.update(db_obj=target, obj_in={"is_active": is_active}) or target
        # Platform admins are typically a single null-tenant membership; flip the
        # identity so login itself is denied. Org admins can share an identity
        # across companies, so login filters inactive memberships instead.
        if target.role == "platform_admin" and target.identity is not None:
            _ = await identity_dao.update(db_obj=target.identity, obj_in={"is_active": is_active})

    await write_admin_audit(
        actor=actor,
        action="user_activate" if is_active else "user_deactivate",
        target_type="user",
        target_id=target.id,
        tenant_id=getattr(target, "tenant_id", None),
        changes={"is_active": field_change(target.is_active, is_active)},
        details={"target_role": target.role, "target_email": getattr(target, "email", None)},
        ip_address=ip_address,
    )
    return updated
