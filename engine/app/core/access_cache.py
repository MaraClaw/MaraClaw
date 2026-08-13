"""Redis + request-local cache for agent access *decisions* (not agent rows)."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextvars import ContextVar
from typing import Any

from app.config import get_settings
from app.core.events import get_redis
from app.core.logging import logger

_REDIS_WAIT_SECONDS = 0.05
_POLICY_VERSION_KEY = "aclver:{agent_id}"
_DECISION_KEY = "acl:v1:{agent_id}:{user_id}"

# (user_id, agent_id) -> (agent, level)
_request_memo: ContextVar[dict[tuple[uuid.UUID, uuid.UUID], tuple[Any, str]] | None] = ContextVar(
    "agent_access_memo",
    default=None,
)

# Queued aclver ops for the top-level connection_ctx (flushed after commit).
_deferred_acl: ContextVar[_DeferredAcl | None] = ContextVar("agent_acl_deferred", default=None)


class _DeferredAcl:
    __slots__ = ("bumps", "drops")

    def __init__(self) -> None:
        self.bumps: set[uuid.UUID] = set()
        self.drops: set[uuid.UUID] = set()


def memo_get(user_id: uuid.UUID, agent_id: uuid.UUID) -> tuple[Any, str] | None:
    memo = _request_memo.get()
    if not memo:
        return None
    return memo.get((user_id, agent_id))


def memo_set(user_id: uuid.UUID, agent_id: uuid.UUID, agent: Any, level: str) -> None:
    memo = _request_memo.get()
    if memo is None:
        memo = {}
        _request_memo.set(memo)
    memo[(user_id, agent_id)] = (agent, level)


def clear_request_memo() -> None:
    _request_memo.set(None)


def _ttl_seconds() -> int:
    return int(getattr(get_settings(), "AGENT_ACCESS_CACHE_TTL_SECONDS", 45) or 0)


def _ver_key(agent_id: uuid.UUID) -> str:
    return _POLICY_VERSION_KEY.format(agent_id=agent_id)


def _dec_key(agent_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return _DECISION_KEY.format(agent_id=agent_id, user_id=user_id)


def _tenant_token(user: Any) -> str:
    tenant_id = getattr(user, "tenant_id", None)
    return str(tenant_id) if tenant_id is not None else ""


async def get_cached_level(user: Any, agent_id: uuid.UUID) -> str | None:
    """Return a cached manage/use level, or None on miss / Redis disabled / error."""
    if _ttl_seconds() <= 0:
        return None
    user_id = getattr(user, "id", None)
    if user_id is None:
        return None
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_REDIS_WAIT_SECONDS)
        pipe = client.pipeline()
        pipe.get(_ver_key(agent_id))
        pipe.get(_dec_key(agent_id, user_id))
        ver_raw, payload = await asyncio.wait_for(pipe.execute(), timeout=_REDIS_WAIT_SECONDS)
    except Exception as exc:
        logger.debug("access_cache miss (redis): {}", type(exc).__name__)
        return None
    if not payload:
        logger.debug("access_cache miss")
        return None
    try:
        data = json.loads(payload)
    except TypeError, ValueError:
        return None
    current_ver = str(ver_raw or "0")
    if str(data.get("ver") or "0") != current_ver:
        logger.debug("access_cache miss (ver)")
        return None
    if data.get("user_role") != getattr(user, "role", None):
        return None
    if data.get("user_tenant_id") != _tenant_token(user):
        return None
    level = data.get("level")
    if level not in {"manage", "use"}:
        return None
    logger.debug("access_cache hit")
    return level


async def read_acl_version(agent_id: uuid.UUID) -> str:
    """Return the current aclver token, or ``\"0\"`` if Redis is down / disabled."""
    if _ttl_seconds() <= 0:
        return "0"
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_REDIS_WAIT_SECONDS)
        ver_raw = await asyncio.wait_for(client.get(_ver_key(agent_id)), timeout=_REDIS_WAIT_SECONDS)
    except Exception as exc:
        logger.debug("access_cache ver skipped: {}", type(exc).__name__)
        return "0"
    return str(ver_raw or "0")


async def set_cached_level(
    user: Any,
    agent_id: uuid.UUID,
    level: str,
    *,
    observed_ver: str | None = None,
) -> None:
    if _ttl_seconds() <= 0 or level not in {"manage", "use"}:
        return
    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_REDIS_WAIT_SECONDS)
        ver_raw = await asyncio.wait_for(client.get(_ver_key(agent_id)), timeout=_REDIS_WAIT_SECONDS)
        current = str(ver_raw or "0")
        if observed_ver is not None and current != observed_ver:
            logger.debug("access_cache set skipped (ver changed)")
            return
        payload = json.dumps(
            {
                "level": level,
                "user_role": getattr(user, "role", None),
                "user_tenant_id": _tenant_token(user),
                "ver": current,
            }
        )
        await asyncio.wait_for(
            client.set(_dec_key(agent_id, user_id), payload, ex=_ttl_seconds()),
            timeout=_REDIS_WAIT_SECONDS,
        )
    except Exception as exc:
        logger.debug("access_cache set skipped: {}", type(exc).__name__)


def begin_deferred_acl() -> Any:
    """Start queuing aclver ops until ``flush_deferred_acl`` / ``end_deferred_acl``."""
    pending = _DeferredAcl()
    return _deferred_acl.set(pending)


def end_deferred_acl(token: Any) -> None:
    _deferred_acl.reset(token)


async def flush_deferred_acl() -> None:
    pending = _deferred_acl.get()
    if pending is None:
        return
    for agent_id in pending.drops:
        await _drop_acl_version_now(agent_id)
    for agent_id in pending.bumps:
        if agent_id not in pending.drops:
            await _incr_acl_version_now(agent_id)
    pending.bumps.clear()
    pending.drops.clear()


async def bump_agent_acl_version(agent_id: uuid.UUID | None) -> None:
    """Invalidate all cached decisions for an agent (INCR, no SCAN).

    When a top-level ``connection_ctx`` is open, the INCR is deferred until
    that scope commits so concurrent readers cannot cache a grant under the
    new version while the revoke is still uncommitted.
    """
    if agent_id is None or _ttl_seconds() <= 0:
        return
    pending = _deferred_acl.get()
    if pending is not None:
        pending.bumps.add(agent_id)
        return
    await _incr_acl_version_now(agent_id)


async def drop_agent_acl_version(agent_id: uuid.UUID | None) -> None:
    if agent_id is None:
        return
    pending = _deferred_acl.get()
    if pending is not None:
        pending.drops.add(agent_id)
        return
    await _drop_acl_version_now(agent_id)


async def _incr_acl_version_now(agent_id: uuid.UUID) -> None:
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_REDIS_WAIT_SECONDS)
        await asyncio.wait_for(client.incr(_ver_key(agent_id)), timeout=_REDIS_WAIT_SECONDS)
    except Exception as exc:
        logger.debug("access_cache bump skipped: {}", type(exc).__name__)


async def _drop_acl_version_now(agent_id: uuid.UUID) -> None:
    try:
        client = await asyncio.wait_for(get_redis(), timeout=_REDIS_WAIT_SECONDS)
        await asyncio.wait_for(client.delete(_ver_key(agent_id)), timeout=_REDIS_WAIT_SECONDS)
    except Exception as exc:
        logger.debug("access_cache drop skipped: {}", type(exc).__name__)
