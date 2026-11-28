"""Bounded replay of committed cache invalidations; no detached retries.

One pass is one claim statement, one non-transactional Redis pipeline, one settle statement and at most one retry
statement, so a full batch holds a pool connection for one statement at a time instead of fifty at once.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, assert_never
from uuid import uuid4

from anyio import fail_after, sleep
from psycopg import Error as PsycopgError
from redis.exceptions import RedisError

from app.core.events import get_cache_redis
from app.core.logging import logger, new_trace_id
from app.core.metrics import Family, counter_family, gauge_family
from app.dao.cache_invalidation_dao import cache_invalidation_dao as outbox
from app.db.errors import DbError
from app.records.cache_invalidation import CacheInvalidationClaim, CacheInvalidationKind
from app.runtime.tasks import capped_backoff, loop_progress

BATCH_SIZE: Final = 50
LEASE_SECONDS: Final = 30
PASS_TIMEOUT_SECONDS: Final = 20
POLL_SECONDS: Final = 1
PROGRESS_BUDGET_SECONDS: Final = 300.0


@dataclass(frozen=True, slots=True)
class ReplayPass:
    """What one pass did: rows claimed, and whether its Redis pipeline ran at all."""

    claimed: int
    reached_redis: bool

    @property
    def drain_again(self) -> bool:
        """A full batch that reached Redis may have a backlog behind it; an unreachable Redis waits the normal poll."""
        return self.reached_redis and self.claimed >= BATCH_SIZE


@dataclass(slots=True)
class _ReplayTally:
    settled: int = 0
    retried: int = 0
    fenced: int = 0
    last_batch_size: int = 0


_tally = _ReplayTally()


def metric_families() -> tuple[Family, ...]:
    """Per-process replay series for a scrape: plain integers, no pool connection and no Redis call."""
    return (
        counter_family(
            "maraclaw_cache_invalidation_settled",
            "Outbox rows acknowledged after their Redis write succeeded.",
            (((), float(_tally.settled)),),
        ),
        counter_family(
            "maraclaw_cache_invalidation_retried",
            "Outbox rows released for a later pass after their Redis write failed.",
            (((), float(_tally.retried)),),
        ),
        counter_family(
            "maraclaw_cache_invalidation_fenced",
            "Claimed outbox rows neither settled nor retried: captured again or reclaimed mid-flight.",
            (((), float(_tally.fenced)),),
        ),
        gauge_family(
            "maraclaw_cache_invalidation_last_batch_size",
            "Rows claimed by the latest replay pass; a full batch means the backlog is at least that deep.",
            (((), float(_tally.last_batch_size)),),
        ),
    )


async def publish_batch(claims: Sequence[CacheInvalidationClaim]) -> list[bool] | None:
    """Write every claim through one non-transactional pipeline of single-key commands; one result per claim.

    A new opaque token on EVERY SET prevents replay from resurrecting an old snapshot. A command error fails only its
    own claim; ``None`` means the pipeline itself failed, so nothing was acknowledged. The write never goes through
    the cache breaker: an invalidation is owed whether or not cache reads are being refused.
    """
    try:
        client = await get_cache_redis()
        async with client.pipeline(transaction=False) as pipe:
            for claim in claims:
                match claim.kind:
                    case (
                        CacheInvalidationKind.ACL_BUMP | CacheInvalidationKind.ACL_DROP | CacheInvalidationKind.VERSION
                    ):
                        _ = pipe.set(claim.key, uuid4().hex, ex=3600)
                    case CacheInvalidationKind.DELETE:
                        _ = pipe.delete(claim.key)
                    case _:
                        assert_never(claim.kind)
            replies = list[object](await pipe.execute(raise_on_error=False))
    except (RedisError, OSError) as exc:
        logger.warning("Cache invalidation replay failed: error={}", type(exc).__name__)
        return None
    return [not isinstance(reply, Exception) for reply in replies]


async def replay_cache_invalidations() -> ReplayPass:
    """Own one bounded batch through settlement. Cancellation leaves claims lease-recoverable."""
    claims = await outbox.claim_batch(limit=BATCH_SIZE, lease_seconds=LEASE_SECONDS)
    _tally.last_batch_size = len(claims)
    if not claims:
        return ReplayPass(claimed=0, reached_redis=False)
    with fail_after(PASS_TIMEOUT_SECONDS):
        written = await publish_batch(claims)
        outcomes = [False] * len(claims) if written is None else written
        published = [claim for claim, ok in zip(claims, outcomes, strict=True) if ok]
        failed = [claim for claim, ok in zip(claims, outcomes, strict=True) if not ok]
        settled = await outbox.settle_batch(published)
        retried = await outbox.retry_batch(failed)
    _tally.settled += settled
    _tally.retried += retried
    _tally.fenced += len(claims) - settled - retried
    return ReplayPass(claimed=len(claims), reached_redis=written is not None)


async def start_cache_invalidation_worker() -> None:
    """Required worker-role loop; readiness beats only after a complete replay pass."""
    progress = loop_progress()
    failures = 0
    while True:
        new_trace_id()
        try:
            replayed = await replay_cache_invalidations()
        except* (DbError, PsycopgError, OSError, TimeoutError) as exc:
            logger.warning("Cache invalidation recovery unavailable: error={}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=1.0, cap_seconds=30.0))
            failures += 1
        else:
            failures = 0
            progress.beat()
            if not replayed.drain_again:
                await sleep(POLL_SECONDS)
