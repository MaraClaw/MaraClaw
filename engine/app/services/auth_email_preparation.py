"""Producer preparation with the existing credential and trusted-origin contracts."""

from datetime import datetime

from fastapi import Request

from app.dao.identity_dao import identity_dao
from app.records.auth_email import AuthEmailKind, AuthEmailMaterial
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services import frontend_origin
from app.services.auth_email_delivery import accept, password_fingerprint
from app.services.email_verification_service import email_verification_service


async def prepare_verification(user: UserRecord, request: Request | None) -> None:
    base_url = await frontend_origin.resolve_frontend_base_url(request)
    if user.identity_id is None:
        return
    identity = await identity_dao.get(user.identity_id)
    if identity is None or identity.email in (None, ""):
        return
    credential, expires_at = await email_verification_service.create_email_verification_token(
        identity.id, identity.email
    )
    url = await email_verification_service.build_email_verification_url(base_url, credential)
    await accept(
        identity,
        AuthEmailMaterial(
            identity.email, user.display_name or identity.email, credential, url, password_fingerprint(identity)
        ),
        kind=AuthEmailKind.VERIFICATION,
        expires_at=expires_at,
    )


async def prepare_reset(identity: IdentityRecord, credential: str, *, url: str, expires_at: datetime) -> None:
    if identity.email is None:
        return
    await accept(
        identity,
        AuthEmailMaterial(identity.email, identity.email, credential, url, password_fingerprint(identity)),
        kind=AuthEmailKind.RESET,
        expires_at=expires_at,
    )
