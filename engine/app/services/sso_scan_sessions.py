import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException

from app.core.security import create_access_token, load_user_principal
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.sso_scan_session import SSOScanSessionRecord
from app.records.user import UserRecord
from app.services.auth_provider import ExternalUserInfo
from app.services.sso_scan_authority import ScanLoginAuthority, ScanLoginAuthorityError


@dataclass(frozen=True, slots=True)
class RedeemedScanLogin:
    access_token: str
    user: UserRecord


class ScanLoginUnavailableError(Exception):
    """The session was consumed, but its principal cannot sign in."""


def parse_scan_state(state: str | None) -> uuid.UUID | None:
    if state is None:
        return None
    try:
        return uuid.UUID(state)
    except ValueError:
        return None


async def load_authorizable_session(session_id: uuid.UUID) -> SSOScanSessionRecord | None:
    session = await sso_scan_session_dao.get(session_id)
    if session is None or session.status not in ("pending", "scanned") or session.expires_at <= datetime.now(UTC):
        return None
    return session


async def authorize_scan_session(session_id: uuid.UUID, *, user: UserRecord, provider_type: str) -> bool:
    return (
        await sso_scan_session_dao.authorize(
            session_id, user_id=user.id, provider_type=provider_type, tenant_id=user.tenant_id
        )
        is not None
    )


async def finalize_scan_login(authority: ScanLoginAuthority, user_info: ExternalUserInfo) -> None:
    """Commit account resolution and scan authorization under the exact provider lock."""
    async with connection_ctx() as db:
        provider = await identity_provider_dao.get_for_update(db, authority.provider_id)
        if (
            provider is None
            or not provider.is_active
            or not provider.sso_login_enabled
            or not authority.matches(provider, await identity_provider_dao.get_revision(authority.provider_id))
            or user_info.provider_type != authority.provider_type
        ):
            raise ScanLoginAuthorityError
        session = await load_authorizable_session(authority.session_id)
        if session is None or session.tenant_id != authority.session_tenant_id:
            raise ScanLoginAuthorityError
        adapter = authority.adapter()
        user, _ = await adapter.find_or_create_user(
            user_info, tenant_id=str(session.tenant_id) if session.tenant_id else None
        )
        resolved_tenant_id = user.tenant_id
        user = await user_dao.get_with_identity(user.id, fresh=True)
        if (
            user is None
            or not user.is_active
            or user.identity is None
            or not user.identity.is_active
            or user.tenant_id != resolved_tenant_id
            or (session.tenant_id is not None and user.tenant_id != session.tenant_id)
        ):
            raise ScanLoginAuthorityError
        tenant_ids = {tid for tid in (provider.tenant_id, session.tenant_id, user.tenant_id) if tid is not None}
        for tenant_id in sorted(tenant_ids):
            if await db.fetchval("SELECT is_active FROM tenants WHERE id = %(id)s", {"id": tenant_id}) is not True:
                raise ScanLoginAuthorityError
        if not await authorize_scan_session(session.id, user=user, provider_type=authority.provider_type):
            # Raising through the owning scope also rolls back identity, membership and admission writes.
            raise ScanLoginAuthorityError


async def redeem_scan_session(session_id: uuid.UUID) -> RedeemedScanLogin | None:
    user_id = await sso_scan_session_dao.redeem(session_id)
    if user_id is None:
        return None
    try:
        user = await load_user_principal(user_id, require_active=True, enforce_password_change=False, fresh=True)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        raise ScanLoginUnavailableError from exc
    if user.identity is None or not user.identity.is_active:
        raise ScanLoginUnavailableError
    return RedeemedScanLogin(access_token=await create_access_token(str(user.id), user.role), user=user)
