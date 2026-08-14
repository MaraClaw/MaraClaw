"""Seed the genesis platform admin from environment variables.

Platform admin credentials come from PLATFORM_ADMIN_EMAIL / PLATFORM_ADMIN_PASSWORD.
The seeded account must change password after the first successful login.

Security rules:
- If a platform admin already exists, leave the DB alone (no password overwrite).
- New identity: hash env password and set must_change_password=True.
- Existing identity: only elevate after verifying PLATFORM_ADMIN_PASSWORD, then force
  password change. Never elevate a disabled identity or an email whose password does
  not match the env secret.
- Membership is null-tenant so disabling a company cannot lock out the platform admin.
"""

from __future__ import annotations

import secrets

from app.config import get_settings
from app.core.logging import logger
from app.core.security import hash_password_async, verify_password_async
from app.dao import identity_dao, participant_dao, user_dao
from app.db.session import connection_ctx
from app.records.identity import IdentityRecord
from app.records.user import UserRecord


class PlatformAdminSeedError(RuntimeError):
    """Fatal bootstrap failure when the genesis platform admin cannot be ensured."""


def _username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].strip().lower() or "platform-admin"
    return local[:100]


async def _unique_username(email: str) -> str:
    base = _username_from_email(email)
    candidate = base
    if not await identity_dao.is_username_taken(candidate):
        return candidate
    for _ in range(8):
        candidate = f"{base}_{secrets.token_hex(3)}"[:100]
        if not await identity_dao.is_username_taken(candidate):
            return candidate
    return f"{base}_{secrets.token_hex(8)}"[:100]


async def _ensure_platform_user(identity: IdentityRecord) -> UserRecord:
    """Ensure a null-tenant platform_admin membership for the identity."""
    users = await user_dao.get_by_identity_id(identity.id, include_identity=True)
    platform_user = next((u for u in users if u.role == "platform_admin"), None)
    if platform_user:
        if not platform_user.is_active:
            platform_user = await user_dao.update(db_obj=platform_user, obj_in={"is_active": True}) or platform_user
        # Prefer null tenant so company disable cannot lock out the platform admin.
        if platform_user.tenant_id is not None:
            platform_user = await user_dao.update(db_obj=platform_user, obj_in={"tenant_id": None}) or platform_user
        platform_user.identity = identity
        return platform_user

    null_tenant_user = next((u for u in users if u.tenant_id is None), None)
    if null_tenant_user:
        updated = await user_dao.update(
            db_obj=null_tenant_user,
            obj_in={"role": "platform_admin", "is_active": True, "registration_source": "bootstrap"},
        )
        user = updated or null_tenant_user
        user.identity = identity
        return user

    user = await user_dao.create(
        obj_in={
            "identity_id": identity.id,
            "tenant_id": None,
            "display_name": identity.username or "Platform Admin",
            "role": "platform_admin",
            "registration_source": "bootstrap",
            "is_active": True,
        }
    )
    await participant_dao.create_for_user(
        user.id,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )
    user.identity = identity
    return user


async def ensure_platform_admin() -> UserRecord:
    """Create or attach the genesis platform admin from env credentials.

    Raises:
        PlatformAdminSeedError: when greenfield install cannot ensure a platform admin.
    """
    settings = get_settings()
    email = (settings.PLATFORM_ADMIN_EMAIL or "").strip().lower()
    password = settings.PLATFORM_ADMIN_PASSWORD or ""

    existing_admin = await user_dao.first_by_role("platform_admin")
    if existing_admin:
        loaded = await user_dao.get_with_identity(existing_admin.id)
        admin = loaded or existing_admin
        logger.info(
            "[startup] Platform admin already present (user_id=%s); skipping env re-seed",
            admin.id,
        )
        return admin

    if not email or not password:
        raise PlatformAdminSeedError(
            "PLATFORM_ADMIN_EMAIL and PLATFORM_ADMIN_PASSWORD are required when no "
            "platform admin exists. Set both env vars to seed the genesis platform admin."
        )
    if len(password) < 6:
        raise PlatformAdminSeedError("PLATFORM_ADMIN_PASSWORD must be at least 6 characters.")

    async with connection_ctx():
        identity = await identity_dao.get_by_email(email)

        if identity:
            if not identity.is_active:
                raise PlatformAdminSeedError(
                    f"Identity {email} exists but is disabled; refusing to auto-elevate. "
                    "Re-enable the account manually or use a different PLATFORM_ADMIN_EMAIL."
                )
            if not identity.password_hash or not await verify_password_async(password, identity.password_hash):
                raise PlatformAdminSeedError(
                    f"Identity {email} already exists but PLATFORM_ADMIN_PASSWORD does not match. "
                    "Refusing to elevate without proving the bootstrap secret."
                )
            identity = (
                await identity_dao.update(
                    db_obj=identity,
                    obj_in={
                        "is_platform_admin": True,
                        "email_verified": True,
                        "must_change_password": True,
                    },
                )
                or identity
            )
            user = await _ensure_platform_user(identity)
            logger.info(
                "[startup] Elevated existing identity %s to platform admin (id=%s); "
                "password change required on next login",
                email,
                user.id,
            )
            return user

        password_hash = await hash_password_async(password)
        username = await _unique_username(email)
        identity = await identity_dao.create_identity(
            email=email,
            username=username,
            password_hash=password_hash,
            is_platform_admin=True,
            email_verified=True,
            must_change_password=True,
        )
        user = await _ensure_platform_user(identity)
        logger.info(
            "[startup] Seeded genesis platform admin %s (id=%s); password change required on first login",
            email,
            user.id,
        )
        return user
