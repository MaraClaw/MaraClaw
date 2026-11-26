"""Short-TTL Redis cache for soul/memory/skill prompt fragments."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import NamedTuple

from app.config import get_settings
from app.core.events import get_cache_redis
from app.core.json_types import json_as_str, json_loads_object
from app.core.logging import logger
from app.core.redis_cache import (
    CacheUnavailableError,
    bump_version,
    cache_delete,
    guarded,
    has_pending_snapshot_writes,
)
from app.core.single_flight import SingleFlight

_DEFAULT_TTL = 60
_loads: SingleFlight[tuple[uuid.UUID, str, str], str] = SingleFlight()


class CachedText(NamedTuple):
    """The cached text of one kind (None on miss or error) and the version observed before it was read."""

    text: str | None
    ver: str


_UNAVAILABLE = CachedText(None, "")


def _ttl() -> int:
    return int(getattr(get_settings(), "AGENT_CONTEXT_CACHE_TTL_SECONDS", _DEFAULT_TTL) or 0)


def _key(agent_id: uuid.UUID, kind: str) -> str:
    return f"ctx:{agent_id}:{kind}"


def _ver_key(agent_id: uuid.UUID, kind: str) -> str:
    return f"ctxver:{agent_id}:{kind}"


def _decode_payload(raw: str | None, current_ver: str) -> str | None:
    if raw is None:
        return None
    try:
        data = json_loads_object(raw)
    except TypeError, ValueError:
        return None
    if data and "v" in data:
        if str(data.get("ver") or "0") != current_ver:
            return None
        return json_as_str(data.get("v"))
    return None


async def read_cached_texts(agent_id: uuid.UUID, kinds: Sequence[str]) -> dict[str, CachedText]:
    """Read every kind's version and text in one round trip under the cache breaker.

    Each text is None on miss / Redis error / open breaker, with an empty version that disables the fill.
    """
    if _ttl() <= 0 or has_pending_snapshot_writes():
        return dict.fromkeys(kinds, _UNAVAILABLE)

    async def read() -> list[object]:
        pipe = (await get_cache_redis()).pipeline(transaction=False)
        for kind in kinds:
            _ = pipe.set(_ver_key(agent_id, kind), uuid.uuid4().hex, ex=max(_ttl() * 40, 3600), nx=True)
            _ = pipe.get(_ver_key(agent_id, kind))
            _ = pipe.get(_key(agent_id, kind))
        return list[object](await pipe.execute())

    try:
        replies = await guarded(read)
    except CacheUnavailableError as exc:
        logger.debug("agent_context_cache get skipped: {}", exc)
        return dict.fromkeys(kinds, _UNAVAILABLE)
    cached: dict[str, CachedText] = {}
    for index, kind in enumerate(kinds):
        ver_raw, value = replies[3 * index + 1], replies[3 * index + 2]
        current = str(ver_raw or "")
        if isinstance(value, bytes):
            value = value.decode()
        cached[kind] = CachedText(_decode_payload(value if isinstance(value, str) else None, current), current)
    return cached


async def read_cached_text(agent_id: uuid.UUID, kind: str) -> CachedText:
    return (await read_cached_texts(agent_id, (kind,)))[kind]


async def get_cached_text(agent_id: uuid.UUID, kind: str) -> str | None:
    return (await read_cached_text(agent_id, kind)).text


async def load_cached_text(
    agent_id: uuid.UUID, kind: str, observed_ver: str, load: Callable[[], Awaitable[str]]
) -> str:
    """Load a missed kind and fill the cache; concurrent misses at one version share the load and the fill.

    Without an observed version there is no cache to fill and nothing to tell an older load from a newer write, so
    the caller loads for itself.
    """
    if not observed_ver:
        return await load()

    async def fill() -> str:
        text = await load()
        await set_cached_text(agent_id, kind, text, observed_ver=observed_ver)
        return text

    return await _loads.run((agent_id, kind, observed_ver), fill)


async def set_cached_text(
    agent_id: uuid.UUID,
    kind: str,
    text: str,
    *,
    observed_ver: str | None = None,
) -> None:
    if _ttl() <= 0 or has_pending_snapshot_writes() or not bool(observed_ver):
        return
    max_bytes = int(getattr(get_settings(), "REDIS_CACHE_MAX_VALUE_BYTES", 65536) or 65536)
    if len(text.encode()) > max_bytes:
        logger.debug("agent_context_cache set skipped (too large)")
        return

    async def fill() -> bool:
        client = await get_cache_redis()
        current = str(await client.get(_ver_key(agent_id, kind)) or "")
        if current != observed_ver:
            return False
        payload = json.dumps({"ver": current, "v": text}, separators=(",", ":"))
        _ = await client.set(_key(agent_id, kind), payload, ex=_ttl())
        return True

    try:
        filled = await guarded(fill)
    except CacheUnavailableError as exc:
        logger.debug("agent_context_cache set skipped: {}", exc)
        return
    if not filled:
        logger.debug("agent_context_cache set skipped (ver)")


async def invalidate_agent_context(agent_id: uuid.UUID, kind: str | None = None) -> None:
    kinds = (kind,) if kind not in (None, "") else ("soul", "memory", "skills")
    if _ttl() <= 0:
        return
    for item in kinds:
        await bump_version(_ver_key(agent_id, item), ttl=_ttl() * 40)
        await cache_delete(_key(agent_id, item), defer_until_commit=True)


def context_kind_for_path(path: str) -> str | None:
    """Map a workspace-relative path to a cache kind, or None if unrelated."""
    normalized = path.replace("\\", "/").strip().lstrip("/")
    lower = normalized.lower()
    if lower == "soul.md":
        return "soul"
    if lower == "memory.md" or lower.startswith("memory/"):
        return "memory"
    if lower == "skills" or lower.startswith("skills/"):
        return "skills"
    return None


async def invalidate_for_workspace_paths(agent_id: uuid.UUID, *paths: str) -> None:
    kinds = {context_kind_for_path(path) for path in paths}
    kinds.discard(None)
    if not kinds:
        return
    if len(kinds) >= 3:
        await invalidate_agent_context(agent_id)
        return
    for kind in kinds:
        await invalidate_agent_context(agent_id, kind)
