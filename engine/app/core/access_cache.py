"""Redis + request-local cache for agent access *decisions* (not agent rows)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import ClassVar, Final, Protocol

from app.config import get_settings
from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.users import UserRole
from app.core.events import get_cache_redis
from app.core.json_types import json_loads_object
from app.core.logging import logger
from app.records.agent import AgentRecord

_POLICY_VERSION_KEY = "aclver:{agent_id}"
_DECISION_KEY = "acl:v1:{agent_id}:{user_id}"
# Compare-and-set fill: write the decision only while the version still equals the observed one.
_FILL_SCRIPT: Final = (
    "if (redis.call('GET', KEYS[1]) or '0') ~= ARGV[1] then return 0 end "
    "redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3]); return 1"
)


@dataclass(frozen=True, slots=True)
class AclSnapshot:
    """Version and raw decision read in one round trip, before any DB read.

    ``version`` is ``None`` when the read was skipped or failed; that disables both the hit and the fill.
    """

    version: str | None
    decision: str | bytes | bytearray | None


NO_ACL_SNAPSHOT: Final = AclSnapshot(None, None)


class _UserLike(Protocol):
    id: uuid.UUID
    role: UserRole
    tenant_id: uuid.UUID | None


# (user_id, agent_id) -> (agent, level)
_request_memo: ContextVar[dict[tuple[uuid.UUID, uuid.UUID], tuple[AgentRecord, PermissionAccessLevel]] | None] = (
    ContextVar(
        "agent_access_memo",
        default=None,
    )
)

# Queued aclver ops for the top-level connection_ctx (flushed after commit).
_deferred_acl: ContextVar[_DeferredAcl | None] = ContextVar("agent_acl_deferred", default=None)


class _DeferredAcl:
    __slots__: ClassVar[tuple[str, ...]] = ("bumps", "drops")

    def __init__(self) -> None:
        self.bumps: set[uuid.UUID] = set()
        self.drops: set[uuid.UUID] = set()


def memo_get(user_id: uuid.UUID, agent_id: uuid.UUID) -> tuple[AgentRecord, PermissionAccessLevel] | None:
    memo = _request_memo.get()
    if memo is None or not memo:
        return None
    return memo.get((user_id, agent_id))


def memo_set(user_id: uuid.UUID, agent_id: uuid.UUID, agent: AgentRecord, level: PermissionAccessLevel) -> None:
    memo = _request_memo.get()
    if memo is None:
        memo = {}
        _ = _request_memo.set(memo)
    memo[(user_id, agent_id)] = (agent, level)


def clear_request_memo() -> None:
    _ = _request_memo.set(None)


def _ttl_seconds() -> int:
    return int(getattr(get_settings(), "AGENT_ACCESS_CACHE_TTL_SECONDS", 45) or 0)


def _ver_key(agent_id: uuid.UUID) -> str:
    return _POLICY_VERSION_KEY.format(agent_id=agent_id)


def _dec_key(agent_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return _DECISION_KEY.format(agent_id=agent_id, user_id=user_id)


def _tenant_token(user: _UserLike) -> str:
    tenant_id = user.tenant_id
    return str(tenant_id) if tenant_id is not None else ""


async def read_acl_snapshot(user_id: uuid.UUID, agent_id: uuid.UUID) -> AclSnapshot:
    """Read the policy version and decision in one pipeline; bounded by the cache socket timeout."""
    if _ttl_seconds() <= 0 or has_pending_acl_writes():
        return NO_ACL_SNAPSHOT
    try:
        client = await get_cache_redis()
        pipe = client.pipeline()
        _ = pipe.get(_ver_key(agent_id))
        _ = pipe.get(_dec_key(agent_id, user_id))
        results = list[object](await pipe.execute())
    except Exception as exc:
        logger.debug("access_cache read skipped: {}", type(exc).__name__)
        return NO_ACL_SNAPSHOT
    if len(results) < 2:
        return NO_ACL_SNAPSHOT
    ver_raw, payload = results[0], results[1]
    return AclSnapshot(str(ver_raw or "0"), payload if isinstance(payload, (str, bytes, bytearray)) else None)


async def get_cached_level(
    user: _UserLike, agent_id: uuid.UUID, snapshot: AclSnapshot | None = None
) -> PermissionAccessLevel | None:
    """Return a cached manage/use level, or None on miss / Redis disabled / error.

    Pass the ``snapshot`` read before the user/agent DB loads so ``user`` can be the freshly loaded row;
    without one this reads its own snapshot.
    """
    if _ttl_seconds() <= 0 or has_pending_acl_writes():
        return None
    if snapshot is None:
        snapshot = await read_acl_snapshot(user.id, agent_id)
    if snapshot.version is None or snapshot.decision is None:
        return None
    try:
        data = json_loads_object(snapshot.decision)
    except TypeError, ValueError:
        return None
    if str(data.get("ver") or "0") != snapshot.version:
        logger.debug("access_cache miss (ver)")
        return None
    if data.get("user_role") != user.role:
        return None
    if data.get("user_tenant_id") != _tenant_token(user):
        return None
    level = data.get("level")
    if level not in {"manage", "use"}:
        return None
    logger.debug("access_cache hit")
    return PermissionAccessLevel(str(level))


async def read_acl_version(agent_id: uuid.UUID) -> str:
    """Return the current aclver token, or ``\"0\"`` if Redis is down / disabled."""
    if _ttl_seconds() <= 0:
        return "0"
    try:
        client = await get_cache_redis()
        ver_raw = await client.get(_ver_key(agent_id))
    except Exception as exc:
        logger.debug("access_cache ver skipped: {}", type(exc).__name__)
        return "0"
    return str(ver_raw or "0")


async def set_cached_level(
    user: _UserLike,
    agent_id: uuid.UUID,
    level: PermissionAccessLevel,
    *,
    observed_ver: str | None = None,
) -> None:
    """Fill the decision only under ``observed_ver``; no fill without one (the read failed or was skipped)."""
    if observed_ver is None or _ttl_seconds() <= 0 or level not in {"manage", "use"} or has_pending_acl_writes():
        return
    payload = json.dumps(
        {"level": level, "user_role": user.role, "user_tenant_id": _tenant_token(user), "ver": observed_ver}
    )
    try:
        client = await get_cache_redis()
        await client.eval(
            _FILL_SCRIPT,
            2,
            _ver_key(agent_id),
            _dec_key(agent_id, user.id),
            observed_ver,
            payload,
            str(_ttl_seconds()),
        )
    except Exception as exc:
        logger.debug("access_cache set skipped: {}", type(exc).__name__)


def has_pending_acl_writes() -> bool:
    pending = _deferred_acl.get()
    return pending is not None and bool(pending.bumps or pending.drops)


def begin_deferred_acl() -> Token[_DeferredAcl | None]:
    """Start queuing aclver ops until ``flush_deferred_acl`` / ``end_deferred_acl``."""
    pending = _DeferredAcl()
    return _deferred_acl.set(pending)


def end_deferred_acl(token: Token[_DeferredAcl | None]) -> None:
    _deferred_acl.reset(token)


async def flush_deferred_acl() -> None:
    """Send every queued bump and drop as one pipeline; a drop wins over a bump of the same agent."""
    pending = _deferred_acl.get()
    if pending is None:
        return
    drops = tuple(pending.drops)
    bumps = tuple(agent_id for agent_id in pending.bumps if agent_id not in pending.drops)
    pending.bumps.clear()
    pending.drops.clear()
    await _invalidate_now(bumps, drops)


async def bump_agent_acl_version(agent_id: uuid.UUID | None) -> None:
    """Invalidate all cached decisions for an agent (INCR, no SCAN).

    When a top-level ``connection_ctx`` is open, the INCR is deferred until
    that scope commits so concurrent readers cannot cache a grant under the
    new version while the revoke is still uncommitted.
    """
    clear_request_memo()
    if agent_id is None:
        return
    pending = _deferred_acl.get()
    if pending is not None:
        pending.bumps.add(agent_id)
        return
    if _ttl_seconds() <= 0:
        return
    await _incr_acl_version_now(agent_id)


async def drop_agent_acl_version(agent_id: uuid.UUID | None) -> None:
    clear_request_memo()
    if agent_id is None:
        return
    pending = _deferred_acl.get()
    if pending is not None:
        pending.drops.add(agent_id)
        return
    await _drop_acl_version_now(agent_id)


async def _invalidate_now(bumps: Collection[uuid.UUID], drops: Collection[uuid.UUID]) -> None:
    """One bounded MULTI/EXEC pipeline: DEL the drops, INCR+EXPIRE the bumps.

    INCR and EXPIRE commit together, so no version key is left without a TTL. Invalidations never consult the
    read circuit; a failure is logged at WARNING and the cache entries age out by TTL.
    """
    if not bumps and not drops:
        return
    ttl = max(_ttl_seconds() * 40, 3600)
    try:
        client = await get_cache_redis()
        pipe = client.pipeline()
        if drops:
            _ = pipe.delete(*(_ver_key(agent_id) for agent_id in drops))
        for agent_id in bumps:
            _ = pipe.incr(_ver_key(agent_id))
            _ = pipe.expire(_ver_key(agent_id), ttl)
        _ = await pipe.execute()
    except Exception as exc:
        logger.warning(
            "access_cache invalidation failed: bumps={} drops={} error={}", len(bumps), len(drops), type(exc).__name__
        )


async def _incr_acl_version_now(agent_id: uuid.UUID) -> None:
    await _invalidate_now((agent_id,), ())


async def _drop_acl_version_now(agent_id: uuid.UUID) -> None:
    await _invalidate_now((), (agent_id,))
