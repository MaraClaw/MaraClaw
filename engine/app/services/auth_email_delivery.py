"""Authenticated mail payloads and read-only current credential eligibility."""

import hashlib
import secrets
from dataclasses import replace
from datetime import UTC, datetime
from typing import Final, assert_never
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import TypeAdapter

from app.config import get_settings
from app.core.events import get_redis
from app.dao.auth_email_dao import auth_email_dao
from app.dao.identity_dao import identity_dao
from app.records.auth_email import MATERIAL, AuthEmailClaim, AuthEmailKind, AuthEmailMaterial, NewAuthEmail
from app.records.identity import IdentityRecord
from app.services import email_verification_service as verification, password_reset_service as reset

_DOMAIN: Final = b"maraclaw:auth-email:v1\0"
_REDIS_VALUES: Final = TypeAdapter(tuple[str | None, str | None])
_VERIFICATION_TOKEN: Final = TypeAdapter(verification.ConsumedEmailVerificationToken)


def password_fingerprint(identity: IdentityRecord) -> str:
    return hashlib.sha256((identity.password_hash or "").encode()).hexdigest()


def _cipher() -> AESGCM:
    return AESGCM(hashlib.sha256(_DOMAIN + get_settings().SECRET_KEY.encode()).digest())


def _binding(job: NewAuthEmail) -> bytes:
    return (
        _DOMAIN
        + job.id.bytes
        + job.identity_id.bytes
        + job.kind.encode()
        + job.expires_at.astimezone(UTC).isoformat().encode()
    )


async def accept(
    identity: IdentityRecord, material: AuthEmailMaterial, *, kind: AuthEmailKind, expires_at: datetime
) -> UUID:
    """Persist the existing credential and trusted URL, without touching its Redis lifetime."""
    job = NewAuthEmail(uuid4(), identity.id, kind, expires_at, b"")
    nonce = secrets.token_bytes(12)
    payload = nonce + _cipher().encrypt(nonce, MATERIAL.dump_json(material), _binding(job))
    await auth_email_dao.enqueue(replace(job, payload=payload))
    return job.id


def unseal(job: NewAuthEmail) -> AuthEmailMaterial:
    raw = _cipher().decrypt(job.payload[:12], job.payload[12:], _binding(job))
    return MATERIAL.validate_json(raw)


async def eligible(claim: AuthEmailClaim, material: AuthEmailMaterial) -> bool:
    """Read both Redis mappings; errors propagate so uncertainty defers rather than sends."""
    identity = await identity_dao.get(claim.identity_id)
    if (
        datetime.now(UTC) >= claim.expires_at
        or identity is None
        or not identity.is_active
        or identity.email != material.to_email
        or password_fingerprint(identity) != material.password_fingerprint
    ):
        return False
    digest = hashlib.sha256(material.credential.encode()).hexdigest()
    match claim.kind:
        case AuthEmailKind.VERIFICATION:
            if identity.email_verified:
                return False
            prefix, user_prefix = verification.EMAIL_VERIFICATION_CODE_KEY_PREFIX, verification.USER_PREFIX
        case AuthEmailKind.RESET:
            prefix, user_prefix = reset.RESET_REDIS_KEY_PREFIX, reset.USER_PREFIX
        case unreachable:
            assert_never(unreachable)
    redis = await get_redis()
    async with redis.pipeline(transaction=True) as pipe:
        _ = pipe.get(f"{prefix}{digest}")
        _ = pipe.get(f"{user_prefix}{identity.id}")
        token_data, current = _REDIS_VALUES.validate_python(await pipe.execute())
    if token_data is None or token_data == "" or current != digest:
        return False
    match claim.kind:
        case AuthEmailKind.VERIFICATION:
            stored = _VERIFICATION_TOKEN.validate_json(token_data)
            return stored["identity_id"] == identity.id and stored["email"] == material.to_email
        case AuthEmailKind.RESET:
            return UUID(token_data) == identity.id
        case unreachable:
            assert_never(unreachable)
