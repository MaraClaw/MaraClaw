"""Invitation code issuing helpers: random codes, server-side expiry, recipient normalization, status."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.records.invitation import InvitationCodeRecord

INVITATION_TTL_MAX_HOURS = 336

type InvitationStatus = Literal["active", "expired", "used", "revoked"]


def new_invitation_code() -> str:
    """128 random bits as 22 URL-safe characters; fits ``invitation_codes.code`` (32)."""
    return secrets.token_urlsafe(16)


def invitation_expiry(ttl_hours: int) -> datetime:
    return datetime.now(UTC) + timedelta(hours=ttl_hours)


def normalize_invite_email(email: str | None) -> str | None:
    """Canonical recipient form shared by issuing and redemption."""
    normalized = (email or "").strip().lower()
    return normalized or None


def invitation_status(invitation: InvitationCodeRecord, now: datetime | None = None) -> InvitationStatus:
    if not invitation.is_active:
        return "revoked"
    if invitation.used_count >= invitation.max_uses:
        return "used"
    if invitation.expires_at is not None and invitation.expires_at <= (now or datetime.now(UTC)):
        return "expired"
    return "active"
