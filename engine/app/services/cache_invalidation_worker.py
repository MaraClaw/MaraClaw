"""Bounded replay of committed cache invalidations; no detached retries."""

from typing import Final, assert_never
from uuid import uuid4

from anyio import create_task_group, fail_after, sleep
from psycopg import Error as PsycopgError
from redis.exceptions import RedisError

from app.core.events import get_cache_redis
from app.core.logging import logger, new_trace_id
from app.dao.cache_invalidation_dao import cache_invalidation_dao as outbox
from app.db.errors import DbError
from app.records.cache_invalidation import CacheInvalidationClaim, CacheInvalidationKind
from app.runtime.tasks import capped_backoff, loop_progress

BATCH_SIZE: Final = 50
LEASE_SECONDS: Final = 30
PASS_TIMEOUT_SECONDS: Final = 20
POLL_SECONDS: Final = 1
PROGRESS_BUDGET_SECONDS: Final = 300.0


async def publish_claim(claim: CacheInvalidationClaim) -> bool:
    """A new opaque token on EVERY attempt prevents replay from resurrecting an old snapshot."""
    try:
        client = await get_cache_redis()
        match claim.kind:
            case CacheInvalidationKind.ACL_BUMP | CacheInvalidationKind.ACL_DROP | CacheInvalidationKind.VERSION:
                return bool(await client.set(claim.key, uuid4().hex, ex=3600))
            case CacheInvalidationKind.DELETE:
                _ = await client.delete(claim.key)
            case unreachable:
                assert_never(unreachable)
    except (RedisError, OSError) as exc:
        logger.warning("Cache invalidation replay failed: error={}", type(exc).__name__)
        return False
    return True


async def _deliver(claim: CacheInvalidationClaim) -> None:
    # Each DAO call exits its SQL transaction before Redis is touched.
    if await publish_claim(claim):
        _ = await outbox.settle(claim)
    else:
        _ = await outbox.retry(claim)


async def replay_cache_invalidations() -> int:
    """Own one bounded batch through settlement. Cancellation leaves claims lease-recoverable."""
    claims = await outbox.claim_batch(limit=BATCH_SIZE, lease_seconds=LEASE_SECONDS)
    with fail_after(PASS_TIMEOUT_SECONDS):
        async with create_task_group() as batch:
            for claim in claims:
                batch.start_soon(_deliver, claim)
    return len(claims)


async def start_cache_invalidation_worker() -> None:
    """Required worker-role loop; readiness beats only after a complete replay pass."""
    progress = loop_progress()
    failures = 0
    while True:
        new_trace_id()
        try:
            await replay_cache_invalidations()
        except* (DbError, PsycopgError, OSError, TimeoutError) as exc:
            logger.warning("Cache invalidation recovery unavailable: error={}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=1.0, cap_seconds=30.0))
            failures += 1
        else:
            failures = 0
            progress.beat()
            await sleep(POLL_SECONDS)
