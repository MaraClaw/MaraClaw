"""Ensure the genesis platform admin exists at process startup.

Startup order:
1. Look for a genesis platform admin in the database with usable login
   credentials (email + password hash).
2. If those credentials are missing, seed from PLATFORM_ADMIN_EMAIL /
   PLATFORM_ADMIN_PASSWORD.
3. If the env vars are also missing, fail closed so the API does not serve
   without an operator.

Security rules:
- A usable genesis row is left alone (no password overwrite).
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
            obj_in={
                "role": "platform_admin",
                "is_active": True,
                "registration_source": "bootstrap",
                "is_genesis": True,
            },
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
            "is_genesis": True,
        }
    )
    await participant_dao.create_for_user(
        user.id,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )
    user.identity = identity
    return user


def _has_login_credentials(user: UserRecord) -> bool:
    identity = getattr(user, "identity", None)
    if identity is None:
        return False
    email = (getattr(identity, "email", None) or "").strip()
    return bool(email and getattr(identity, "password_hash", None))


async def _find_genesis_membership() -> UserRecord | None:
    """Return the persisted genesis PA, backfilling the flag on the earliest PA."""
    genesis = await user_dao.genesis_platform_admin()
    if genesis is not None:
        return genesis
    earliest = await user_dao.first_by_role("platform_admin")
    if earliest is None:
        return None
    if not getattr(earliest, "is_genesis", False):
        earliest = await user_dao.update(db_obj=earliest, obj_in={"is_genesis": True}) or earliest
        earliest.is_genesis = True
    return earliest


async def _load_genesis_with_credentials() -> UserRecord | None:
    """Return the genesis PA only when the identity can log in with email + password."""
    genesis = await _find_genesis_membership()
    if genesis is None:
        return None
    loaded = await user_dao.get_with_identity(genesis.id) or genesis
    if _has_login_credentials(loaded):
        return loaded
    return None


async def _repair_genesis_from_env(genesis: UserRecord, *, email: str, password: str) -> UserRecord:
    """Attach missing email/password to an existing genesis PA from env."""
    loaded = await user_dao.get_with_identity(genesis.id) or genesis
    identity = loaded.identity
    if identity is None and loaded.identity_id:
        identity = await identity_dao.get(loaded.identity_id)

    password_hash = await hash_password_async(password)
    if identity is None:
        taken = await identity_dao.get_by_email(email)
        if taken is not None:
            raise PlatformAdminSeedError(
                f"Cannot attach PLATFORM_ADMIN_EMAIL {email} to the genesis platform admin; "
                "that email already belongs to another identity."
            )
        username = await _unique_username(email)
        identity = await identity_dao.create_identity(
            email=email,
            username=username,
            password_hash=password_hash,
            is_platform_admin=True,
            email_verified=True,
            must_change_password=True,
        )
        await user_dao.update(db_obj=loaded, obj_in={"identity_id": identity.id, "is_genesis": True})
        return await _ensure_platform_user(identity)

    updates: dict[str, object] = {
        "is_platform_admin": True,
        "email_verified": True,
        "must_change_password": True,
    }
    if not identity.password_hash:
        updates["password_hash"] = password_hash
    if not (identity.email or "").strip():
        taken = await identity_dao.get_by_email(email)
        if taken is not None and taken.id != identity.id:
            raise PlatformAdminSeedError(
                f"Cannot attach PLATFORM_ADMIN_EMAIL {email} to the genesis platform admin; "
                "that email already belongs to another identity."
            )
        updates["email"] = email
    identity = await identity_dao.update(db_obj=identity, obj_in=updates) or identity
    return await _ensure_platform_user(identity)


async def ensure_platform_admin() -> UserRecord:
    """Ensure genesis platform-admin credentials exist (database, then env).

    Raises:
        PlatformAdminSeedError: when neither the database nor env can supply them.
    """
    existing = await _load_genesis_with_credentials()
    if existing is not None:
        settings = get_settings()
        env_email = (settings.PLATFORM_ADMIN_EMAIL or "").strip().lower()
        actual = (getattr(getattr(existing, "identity", None), "email", None) or "").strip().lower()
        if env_email and actual and env_email != actual:
            logger.warning(
                "[startup] PLATFORM_ADMIN_EMAIL=%s does not match genesis platform admin %s; "
                "env seed skipped because the database already has usable credentials. "
                "Sign in with the genesis email (admin console).",
                env_email,
                actual,
            )
        else:
            logger.info(
                "[startup] Genesis platform admin credentials found in database (user_id=%s); skipping env seed",
                existing.id,
            )
        return existing

    settings = get_settings()
    email = (settings.PLATFORM_ADMIN_EMAIL or "").strip().lower()
    password = settings.PLATFORM_ADMIN_PASSWORD or ""

    if not email or not password:
        raise PlatformAdminSeedError(
            "Genesis platform admin credentials were not found in the database. "
            "Set PLATFORM_ADMIN_EMAIL and PLATFORM_ADMIN_PASSWORD to seed the "
            "genesis platform admin."
        )
    if len(password) < 6:
        raise PlatformAdminSeedError("PLATFORM_ADMIN_PASSWORD must be at least 6 characters.")

    existing_genesis = await _find_genesis_membership()

    async with connection_ctx():
        if existing_genesis is not None:
            user = await _repair_genesis_from_env(existing_genesis, email=email, password=password)
            logger.info(
                "[startup] Repaired genesis platform admin credentials from env (user_id=%s); "
                "password change required on next login",
                user.id,
            )
            return user
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
