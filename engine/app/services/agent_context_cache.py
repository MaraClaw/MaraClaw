"""Short-TTL Redis cache for soul/memory/skill prompt fragments."""

from __future__ import annotations

import asyncio
import json
import uuid

from app.config import get_settings
from app.core.events import get_redis
from app.core.json_types import json_as_str, json_loads_object
from app.core.logging import logger

_DEFAULT_TTL = 60


def _ttl() -> int:
    return int(getattr(get_settings(), "AGENT_CONTEXT_CACHE_TTL_SECONDS", _DEFAULT_TTL) or 0)


def _wait() -> float:
    return float(getattr(get_settings(), "REDIS_CACHE_WAIT_SECONDS", 0.2) or 0.2)


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
        return raw
    if data and "v" in data:
        if str(data.get("ver") or "0") != current_ver:
            return None
        return json_as_str(data.get("v"))
    return raw if isinstance(raw, str) else None


async def read_cached_text(agent_id: uuid.UUID, kind: str) -> tuple[str | None, str]:
    """Return ``(text, observed_ver)``. Text is None on miss / Redis error."""
    if _ttl() <= 0:
        return None, "0"
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_wait())
        ver_raw = await asyncio.wait_for(client.get(_ver_key(agent_id, kind)), timeout=_wait())
        value = await asyncio.wait_for(client.get(_key(agent_id, kind)), timeout=_wait())
    except Exception as exc:
        logger.debug("agent_context_cache get skipped: {}", type(exc).__name__)
        return None, "0"
    current = str(ver_raw or "0")
    if isinstance(value, bytes):
        value = value.decode()
    return _decode_payload(value if isinstance(value, str) else None, current), current


async def get_cached_text(agent_id: uuid.UUID, kind: str) -> str | None:
    text, _ver = await read_cached_text(agent_id, kind)
    return text


async def set_cached_text(
    agent_id: uuid.UUID,
    kind: str,
    text: str,
    *,
    observed_ver: str | None = None,
) -> None:
    if _ttl() <= 0:
        return
    max_bytes = int(getattr(get_settings(), "REDIS_CACHE_MAX_VALUE_BYTES", 65536) or 65536)
    if len(text.encode()) > max_bytes:
        logger.debug("agent_context_cache set skipped (too large)")
        return
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_wait())
        ver_raw = await asyncio.wait_for(client.get(_ver_key(agent_id, kind)), timeout=_wait())
        current = str(ver_raw or "0")
        if observed_ver is not None and current != observed_ver:
            logger.debug("agent_context_cache set skipped (ver)")
            return
        payload = json.dumps({"ver": current, "v": text}, separators=(",", ":"))
        _ = await asyncio.wait_for(client.set(_key(agent_id, kind), payload, ex=_ttl()), timeout=_wait())
    except Exception as exc:
        logger.debug("agent_context_cache set skipped: {}", type(exc).__name__)


async def invalidate_agent_context(agent_id: uuid.UUID, kind: str | None = None) -> None:
    kinds = (kind,) if kind else ("soul", "memory", "skills")
    if _ttl() <= 0:
        return
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_wait())
        _ = await asyncio.wait_for(
            client.delete(*(_key(agent_id, item) for item in kinds)),
            timeout=_wait(),
        )
        for item in kinds:
            _ = await asyncio.wait_for(client.incr(_ver_key(agent_id, item)), timeout=_wait())
    except Exception as exc:
        logger.debug("agent_context_cache invalidate skipped: {}", type(exc).__name__)


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
