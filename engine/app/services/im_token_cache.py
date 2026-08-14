"""Shared Redis cache for IM provider access tokens (Feishu / WeCom / DingTalk).

Keys hash the public id **and** the secret so a caller cannot reuse another
app's token. Tokens are bearer secrets — never log them. Redis errors fall
open to a fresh HTTP mint.
"""

from __future__ import annotations

import hashlib

from app.config import get_settings
from app.core.redis_cache import cache_delete, cache_get, cache_key, cache_set


def _ttl_enabled() -> bool:
    return int(getattr(get_settings(), "IM_TOKEN_CACHE_TTL_SECONDS", 1) or 0) > 0


def _subject_key(provider: str, subject: str, secret: str = "") -> str:
    digest = hashlib.sha256(f"{subject}\0{secret}".encode()).hexdigest()[:16]
    return cache_key("imtok", provider, digest)


async def get_cached_im_token(provider: str, subject: str, *, secret: str = "") -> str | None:
    if not subject or not _ttl_enabled():
        return None
    value = await cache_get(_subject_key(provider, subject, secret))
    return value or None


async def set_cached_im_token(
    provider: str,
    subject: str,
    token: str,
    *,
    ttl: int,
    secret: str = "",
) -> None:
    if not subject or not token or not _ttl_enabled():
        return
    safe_ttl = int(ttl)
    if safe_ttl <= 0:
        return
    await cache_set(_subject_key(provider, subject, secret), token, ttl=safe_ttl)


async def drop_cached_im_token(provider: str, subject: str, *, secret: str = "") -> None:
    if not subject:
        return
    await cache_delete(_subject_key(provider, subject, secret))


def refresh_ttl(expires_in: int | None, *, skew: int = 300) -> int:
    """TTL so we refresh a few minutes before the provider expiry."""
    raw = int(expires_in or 7200)
    return max(raw - skew, 1)
