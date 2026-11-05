"""Initial org-admin credential material: generation, authenticated sealing, staleness and message text."""

import hashlib
import secrets
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings
from app.records.identity import IdentityRecord

SUBJECT = "Your MaraClaw organization is ready"
_KEY_DOMAIN = b"maraclaw:organization-admission:credential:v1\0"


def new_password() -> str:
    """144 bits of entropy; the plaintext exists only in memory and in the intended email."""
    return secrets.token_urlsafe(18)


def fingerprint(password_hash: str) -> str:
    """Digest of the stored bcrypt hash, used only to tell whether the issued password is still current."""
    return hashlib.sha256(password_hash.encode()).hexdigest()


def _cipher() -> AESGCM:
    return AESGCM(hashlib.sha256(_KEY_DOMAIN + get_settings().SECRET_KEY.encode()).digest())


def seal(request_id: UUID, password: str) -> bytes:
    """AES-GCM bound to the request, so a payload cannot be replayed onto another request's intent."""
    nonce = secrets.token_bytes(12)
    return nonce + _cipher().encrypt(nonce, password.encode(), request_id.bytes)


def unseal(request_id: UUID, payload: bytes) -> str:
    return _cipher().decrypt(payload[:12], payload[12:], request_id.bytes).decode()


def obsolete_reason(identity: IdentityRecord | None, issued_fingerprint: str) -> str | None:
    """Static code when the issued credential must never be sent (account gone or password already changed)."""
    if identity is None or not identity.is_active or identity.password_hash is None:
        return "account_unavailable"
    if not identity.must_change_password or fingerprint(identity.password_hash) != issued_fingerprint:
        return "password_changed"
    return None


def message_body(*, organization_name: str, email: str, password: str) -> str:
    """Plain text on purpose: nothing in it is interpreted as markup, and no host is invented."""
    return (
        "Hello,\n\n"
        f"Your organization request was approved. The MaraClaw organization '{organization_name}' has been "
        f"created and an administrator account was set up for {email}.\n\n"
        f"Temporary password: {password}\n\n"
        "Sign in to the MaraClaw admin console with this email address and the temporary password. "
        "You must choose a new password immediately after your first sign-in, and this temporary password "
        "stops working once you do.\n\n"
        "If you did not request this organization, contact MaraClaw support."
    )
