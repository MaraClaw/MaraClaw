"""Shared Redis cache for IM provider access tokens (Feishu / WeCom / DingTalk).

Keys hash the public id **and** the secret so a caller cannot reuse another
app's token. Tokens are bearer secrets - never log them. Redis errors fall
open to a fresh HTTP mint.

``get_or_mint_im_token`` is the one read path: a cache miss starts a single mint
per key per event loop in its own task, so a cancelled caller cannot fail the
callers waiting on it. A failure goes only to callers already waiting and is never cached.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.core import redis_cache
from app.core.logging import logger
from app.core.redis_cache import cache_delete, cache_get, cache_key, cache_set

_DROP_IF_EQUALS = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"


@dataclass(frozen=True, slots=True)
class ImTokenKey:
    provider: str
    subject: str
    secret: str = ""


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A mint result; an empty ``token`` is returned to the waiters but never cached."""

    token: str
    expires_in: int | None = None


class ImTokenRefusedError(Exception):
    """The provider answered a mint without a usable token; callers translate ``response`` to their own contract."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"IM token refused (HTTP {response.status_code})")
        self.response: httpx.Response = response


_inflight: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Task[str]] = {}


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
    _ = await cache_set(_subject_key(provider, subject, secret), token, ttl=safe_ttl)


async def drop_cached_im_token(provider: str, subject: str, *, secret: str = "") -> None:
    if not subject:
        return
    await cache_delete(_subject_key(provider, subject, secret))


async def drop_rejected_im_token(key: ImTokenKey, rejected: str) -> None:
    """Drop the cached token only while it still equals the one the provider rejected."""
    if not key.subject or not rejected:
        return
    try:
        client = await redis_cache._client()
        await client.eval(_DROP_IF_EQUALS, 1, _subject_key(key.provider, key.subject, key.secret), rejected)
    except Exception as exc:
        logger.debug("im_token_cache compare-and-delete skipped: {}", type(exc).__name__)


async def get_or_mint_im_token(key: ImTokenKey, mint: Callable[[], Awaitable[MintedToken]]) -> str:
    """Cached token, else the result of the one in-flight ``mint`` for this key on this event loop."""
    if not key.subject:
        return (await mint()).token
    slot = (asyncio.get_running_loop(), _subject_key(key.provider, key.subject, key.secret))
    task = _inflight.get(slot)
    if task is None:
        cached = await get_cached_im_token(key.provider, key.subject, secret=key.secret)
        if cached is not None:
            return cached
        task = _inflight.get(slot)
        if task is None:
            task = asyncio.create_task(_mint_and_store(key, mint, slot))
            task.add_done_callback(_retrieve_failure)
            _inflight[slot] = task
    return await asyncio.shield(task)


async def _mint_and_store(
    key: ImTokenKey, mint: Callable[[], Awaitable[MintedToken]], slot: tuple[asyncio.AbstractEventLoop, str]
) -> str:
    try:
        cached = await get_cached_im_token(key.provider, key.subject, secret=key.secret)
        if cached is not None:
            return cached
        minted = await mint()
        if minted.token:
            await set_cached_im_token(
                key.provider, key.subject, minted.token, secret=key.secret, ttl=refresh_ttl(minted.expires_in)
            )
        return minted.token
    finally:
        _ = _inflight.pop(slot, None)


def _retrieve_failure(task: asyncio.Task[str]) -> None:
    if not task.cancelled():
        _ = task.exception()


def refresh_ttl(expires_in: int | None, *, skew: int = 300) -> int:
    """TTL so we refresh a few minutes before the provider expiry."""
    raw = int(expires_in or 7200)
    return max(raw - skew, 1)
