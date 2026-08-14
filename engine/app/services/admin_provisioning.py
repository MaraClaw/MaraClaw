"""Create additional platform / org admins. Only genesis admins may do this."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
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


async def is_genesis_platform_admin(user: UserRecord) -> bool:
    """True when ``user`` is the earliest platform_admin membership."""
    if getattr(user, "role", None) != "platform_admin":
        return False
    genesis = await user_dao.first_by_role("platform_admin")
    return genesis is not None and genesis.id == user.id


async def is_genesis_org_admin(user: UserRecord) -> bool:
    """True when ``user`` is the earliest org_admin in their tenant."""
    if getattr(user, "role", None) != "org_admin" or not getattr(user, "tenant_id", None):
        return False
    genesis = await user_dao.first_org_admin_for_tenant(user.tenant_id)
    return genesis is not None and genesis.id == user.id


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
                }
            )
            user.identity = identity
            await participant_dao.create_for_user(
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
                    "quota_message_limit": tenant.default_message_limit,
                    "quota_message_period": tenant.default_message_period,
                    "quota_max_agents": tenant.default_max_agents,
                    "quota_agent_ttl_hours": tenant.default_agent_ttl_hours,
                }
            )
            user.identity = identity
            await participant_dao.create_for_user(
                user.id,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
            )
            from app.services.registration_service import registration_service

            await registration_service.bind_org_member(user)
    except UniqueViolationError as exc:
        raise AdminEmailTakenError(email) from exc

    return ProvisionedAdmin(user=user, admin_email=email)


class AdminActivationError(Exception):
    """Peer admin activate/deactivate is not allowed."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


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

    if actor.role == "platform_admin":
        if not await is_genesis_platform_admin(actor):
            raise AdminActivationError(
                403, "Only the genesis platform admin can activate or deactivate platform admins"
            )
        if target.role != "platform_admin":
            raise AdminActivationError(400, "Target must be a platform admin")
        if await is_genesis_platform_admin(target):
            raise AdminActivationError(403, "Cannot deactivate the genesis platform admin")
        if not is_active and target.is_active:
            active_count = await user_dao.count_active_by_role("platform_admin")
            if active_count <= 1:
                raise AdminActivationError(400, "Cannot deactivate the only active platform admin")
    elif actor.role == "org_admin":
        if not await is_genesis_org_admin(actor):
            raise AdminActivationError(403, "Only the genesis organization admin can activate or deactivate org admins")
        if target.role != "org_admin" or target.tenant_id != actor.tenant_id:
            raise AdminActivationError(403, "Can only change org admins in your own company")
        if await is_genesis_org_admin(target):
            raise AdminActivationError(403, "Cannot deactivate the genesis organization admin")
        if not is_active and target.is_active:
            active_count = await user_dao.count_active_by_role("org_admin", tenant_id=actor.tenant_id)
            if active_count <= 1:
                raise AdminActivationError(400, "Cannot deactivate the only active organization admin")
    else:
        raise AdminActivationError(403, "Admin access required")

    if target.is_active == is_active:
        return target

    updated = await user_dao.update(db_obj=target, obj_in={"is_active": is_active}) or target
    if target.role == "platform_admin" and target.identity is not None:
        await identity_dao.update(db_obj=target.identity, obj_in={"is_active": is_active})

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
