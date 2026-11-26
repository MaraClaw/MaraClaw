"""Redis-backed short-lived locks for workspace mutations.

A lock lasts ``ttl_seconds`` unless renewed. ``workspace_locks`` renews every lock it holds while its body runs, so an
operation that outlives one TTL (a folder of thousands of files) keeps its locks, and stops the moment the body ends.
A renewal that finds a lock gone or held by another owner marks the lease lost; the body learns of it from
``check_workspace_lock`` and stops the work it has left.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol, cast

from redis.exceptions import RedisError

from app.core.events import get_redis
from app.core.logging import logger

LOCK_PREFIX = "workspace-lock"
DEFAULT_LOCK_TTL_SECONDS = 60
RENEWALS_PER_TTL = 3

_RELEASE_IF_OWNER_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_IF_OWNER_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class WorkspaceLockLostError(RuntimeError):
    """A renewal found a held lock gone or taken by another owner, so the operation no longer excludes anyone."""

    def __init__(self, path: str) -> None:
        self.path: str = path
        super().__init__(f"Workspace lock lost: {path}")


class LockClock(Protocol):
    """Time as the renewal loop sees it: a monotonic reading and a sleep, so tests advance both without waiting."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


SYSTEM_CLOCK = MonotonicClock()


@dataclass(slots=True)
class _Lease:
    lost_path: str | None = None


_active_lease: ContextVar[_Lease | None] = ContextVar("workspace_lock_lease", default=None)


def _normalize_workspace_path(path: str) -> str:
    clean = (path or "").replace("\\", "/").strip().lstrip("/")
    parts: list[str] = []
    for part in clean.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                _ = parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _lock_key(agent_id: uuid.UUID, path: str) -> str:
    normalized = _normalize_workspace_path(path) or "."
    return f"{LOCK_PREFIX}:{agent_id}:{normalized}"


async def acquire_workspace_lock(
    agent_id: uuid.UUID,
    path: str,
    *,
    owner_token: str,
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
) -> bool:
    redis = await get_redis()
    return bool(await redis.set(_lock_key(agent_id, path), owner_token, ex=ttl_seconds, nx=True))


async def release_workspace_lock(agent_id: uuid.UUID, path: str, *, owner_token: str) -> None:
    redis = await get_redis()
    await redis.eval(_RELEASE_IF_OWNER_SCRIPT, 1, _lock_key(agent_id, path), owner_token)


async def renew_workspace_lock(
    agent_id: uuid.UUID, path: str, *, owner_token: str, ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS
) -> bool:
    """Extend the lock's expiry to a full TTL; ``False`` when the lock is gone or another owner holds it."""
    redis = await get_redis()
    renewed = cast(
        "object", await redis.eval(_RENEW_IF_OWNER_SCRIPT, 1, _lock_key(agent_id, path), owner_token, ttl_seconds)
    )
    return renewed == 1


def check_workspace_lock() -> None:
    """Raise ``WorkspaceLockLostError`` when a lock held by the surrounding ``workspace_locks`` body was lost.

    A long operation calls this between its steps; outside any lock it does nothing.
    """
    lease = _active_lease.get()
    if lease is not None and lease.lost_path is not None:
        raise WorkspaceLockLostError(lease.lost_path)


async def _renew_while_held(
    lease: _Lease, agent_id: uuid.UUID, paths: list[str], owner_token: str, ttl_seconds: int, clock: LockClock
) -> None:
    """Renew every held lock each third of its TTL until cancelled, or until one is lost.

    A Redis failure (a busy command pool included) leaves the locks as they are: they still hold until their TTL runs
    out, so the next renewal tries again, and only a full TTL without a successful renewal counts as lost.
    """
    renewed_at = clock.now()
    while True:
        await clock.sleep(ttl_seconds / RENEWALS_PER_TTL)
        try:
            for path in paths:
                if not await renew_workspace_lock(agent_id, path, owner_token=owner_token, ttl_seconds=ttl_seconds):
                    lease.lost_path = path
                    return
        except RedisError as exc:
            logger.warning(f"[workspace-lock] renewal failed for {agent_id}: {type(exc).__name__}")
            if clock.now() - renewed_at >= ttl_seconds:
                lease.lost_path = paths[0]
                return
        else:
            renewed_at = clock.now()


@asynccontextmanager
async def workspace_locks(
    agent_id: uuid.UUID,
    paths: list[str],
    *,
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    clock: LockClock = SYSTEM_CLOCK,
):
    normalized = sorted({_normalize_workspace_path(path) or "." for path in paths if cast("object", path) is not None})
    owner_token = uuid.uuid4().hex
    acquired: list[str] = []
    try:
        for path in normalized:
            ok = await acquire_workspace_lock(
                agent_id,
                path,
                owner_token=owner_token,
                ttl_seconds=ttl_seconds,
            )
            if not ok:
                raise RuntimeError(f"Workspace lock busy: {path}")
            acquired.append(path)
        lease = _Lease()
        renewal = asyncio.create_task(_renew_while_held(lease, agent_id, acquired, owner_token, ttl_seconds, clock))
        marker = _active_lease.set(lease)
        try:
            yield
        finally:
            _active_lease.reset(marker)
            _ = renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
    finally:
        for path in reversed(acquired):
            await release_workspace_lock(agent_id, path, owner_token=owner_token)
