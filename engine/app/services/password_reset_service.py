"""Password reset token lifecycle helpers."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from app.config import get_settings
from app.core.events import get_redis

# Key prefixes for Redis
RESET_REDIS_KEY_PREFIX = "pwd_reset:token:"
USER_PREFIX = "pwd_reset:user:"


class ConsumedPasswordResetToken(TypedDict):
    identity_id: uuid.UUID


def _hash_token(token: str) -> str:
    """Hash a raw reset token before persistence or lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_password_reset_token(identity_id: uuid.UUID) -> tuple[str, datetime]:
    """Create a new single-use token and invalidate older unused tokens in Redis."""
    redis = await get_redis()
    user_key = f"{USER_PREFIX}{identity_id}"

    # Invalidate previous token for this user if exists
    old_token_hash = await redis.get(user_key)
    if old_token_hash:
        _ = await redis.delete(f"{RESET_REDIS_KEY_PREFIX}{old_token_hash}")

    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)

    now = datetime.now(UTC)
    expiry_minutes = get_settings().PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    expires_at = now + timedelta(minutes=expiry_minutes)

    # Store the new token (bi-directional mapping for easy invalidation)
    token_key = f"{RESET_REDIS_KEY_PREFIX}{token_hash}"
    ttl_seconds = int(expiry_minutes * 60)

    async with redis.pipeline(transaction=True) as pipe:
        _ = pipe.setex(token_key, ttl_seconds, str(identity_id))
        _ = pipe.setex(user_key, ttl_seconds, token_hash)
        _ = await pipe.execute()

    return raw_token, expires_at


async def get_public_base_url() -> str:
    """Resolve the public base URL used for user-facing links."""
    from app.services.frontend_origin import resolve_frontend_base_url

    return await resolve_frontend_base_url()


async def build_password_reset_url(raw_token: str, request: object | None = None) -> str:
    """Build the user-facing reset URL.

    When ``request`` carries an Origin/Referer listed in CORS_ORIGINS, that
    frontend wins so member reset links land on web-l and admin links on web-a.
    """
    from fastapi import Request

    from app.services.frontend_origin import resolve_frontend_base_url

    req = request if isinstance(request, Request) else None
    base_url = await resolve_frontend_base_url(req)
    return f"{base_url}/reset-password?token={raw_token}"


async def consume_password_reset_token(raw_token: str) -> ConsumedPasswordResetToken | None:
    """Load a valid reset token from Redis and mark it used (by deleting)."""
    redis = await get_redis()
    token_hash = _hash_token(raw_token)
    token_key = f"{RESET_REDIS_KEY_PREFIX}{token_hash}"

    identity_id_str = await redis.get(token_key)
    if not identity_id_str:
        return None

    identity_id_value = identity_id_str.decode("utf-8") if isinstance(identity_id_str, bytes) else identity_id_str
    identity_id = uuid.UUID(identity_id_value)
    user_key = f"{USER_PREFIX}{identity_id}"

    # Atomic delete to ensure single-use
    async with redis.pipeline(transaction=True) as pipe:
        _ = pipe.delete(token_key)
        _ = pipe.delete(user_key)
        _ = await pipe.execute()

    return {"identity_id": identity_id}
