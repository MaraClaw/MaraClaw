import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException

from app.core.security import create_access_token, load_user_principal
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.records.sso_scan_session import SSOScanSessionRecord
from app.records.user import UserRecord


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
    return RedeemedScanLogin(access_token=create_access_token(str(user.id), user.role), user=user)
