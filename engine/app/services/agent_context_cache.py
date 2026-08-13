"""Short-TTL Redis cache for soul/memory/skill prompt fragments."""

from __future__ import annotations

import asyncio
import uuid

from app.config import get_settings
from app.core.events import get_redis
from app.core.logging import logger

_REDIS_WAIT_SECONDS = 0.05
_DEFAULT_TTL = 60


def _ttl() -> int:
    return int(getattr(get_settings(), "AGENT_CONTEXT_CACHE_TTL_SECONDS", _DEFAULT_TTL) or 0)


def _key(agent_id: uuid.UUID, kind: str) -> str:
    return f"ctx:{agent_id}:{kind}"


async def get_cached_text(agent_id: uuid.UUID, kind: str) -> str | None:
    if _ttl() <= 0:
        return None
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_REDIS_WAIT_SECONDS)
        value = await asyncio.wait_for(client.get(_key(agent_id, kind)), timeout=_REDIS_WAIT_SECONDS)
    except Exception as exc:
        logger.debug("agent_context_cache get skipped: {}", type(exc).__name__)
        return None
    return value if isinstance(value, str) else None


async def set_cached_text(agent_id: uuid.UUID, kind: str, text: str) -> None:
    if _ttl() <= 0:
        return
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_REDIS_WAIT_SECONDS)
        await asyncio.wait_for(client.set(_key(agent_id, kind), text, ex=_ttl()), timeout=_REDIS_WAIT_SECONDS)
    except Exception as exc:
        logger.debug("agent_context_cache set skipped: {}", type(exc).__name__)


async def invalidate_agent_context(agent_id: uuid.UUID, kind: str | None = None) -> None:
    kinds = (kind,) if kind else ("soul", "memory", "skills")
    if _ttl() <= 0:
        return
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_REDIS_WAIT_SECONDS)
        await asyncio.wait_for(
            client.delete(*(_key(agent_id, item) for item in kinds)),
            timeout=_REDIS_WAIT_SECONDS,
        )
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
