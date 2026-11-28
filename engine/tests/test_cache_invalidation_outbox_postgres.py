import asyncio
from collections.abc import Sequence
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.core import access_cache, redis_cache
from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.users import UserRole
from app.dao.cache_invalidation_dao import cache_invalidation_dao as outbox
from app.db import session
from app.db.connection import DbConnection, Params, Row
from app.records.cache_invalidation import CacheInvalidation, CacheInvalidationClaim, CacheInvalidationKind
from app.records.user import UserRecord
from app.services import agent_context_cache, cache_invalidation_worker as worker
from cache_invalidation_fixtures import CacheReplayResources

pytest_plugins = ("cache_invalidation_fixtures",)

_COUNTERS = (
    "maraclaw_cache_invalidation_settled_total",
    "maraclaw_cache_invalidation_retried_total",
    "maraclaw_cache_invalidation_fenced_total",
)


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The leading keyword of every statement a ``fetchall`` runs, in order; clear it before the call measured."""
    seen: list[str] = []
    fetchall = DbConnection.fetchall

    async def recording(self: DbConnection, query: str, params: Params = None) -> list[Row]:
        seen.append(query.split()[0])
        return await fetchall(self, query, params)

    monkeypatch.setattr(DbConnection, "fetchall", recording)
    return seen


def version_ops(count: int) -> list[CacheInvalidation]:
    return [CacheInvalidation(f"version:{uuid4()}", CacheInvalidationKind.VERSION) for _ in range(count)]


async def capture(operations: list[CacheInvalidation]) -> None:
    async with session.connection_ctx() as db:
        await outbox.capture(db, operations)


def reading(name: str) -> float:
    return next(sample.value for family in worker.metric_families() for sample in family.samples if sample.name == name)


@pytest.mark.asyncio
async def test_a_pass_is_one_claim_one_non_transactional_pipeline_and_one_settle(
    cache_replay: CacheReplayResources, monkeypatch: pytest.MonkeyPatch, statements: list[str]
) -> None:
    versions, deleted = version_ops(3), CacheInvalidation(f"snapshot:{uuid4()}", CacheInvalidationKind.DELETE)
    await cache_replay.redis.set(deleted.key, "old")
    await capture([*versions, deleted])
    transactional: list[bool] = []
    pipeline = cache_replay.redis.pipeline

    def recording(transaction: bool = True, shard_hint: str | None = None):
        transactional.append(transaction)
        return pipeline(transaction=transaction, shard_hint=shard_hint)

    monkeypatch.setattr(cache_replay.redis, "pipeline", recording)
    statements.clear()
    settled = reading(_COUNTERS[0])

    assert await worker.replay_cache_invalidations() == worker.ReplayPass(claimed=4, reached_redis=True)

    assert statements == ["WITH", "DELETE"]
    assert transactional == [False]
    tokens = [await cache_replay.redis.get(op.key) for op in versions]
    assert all(token is not None and len(token) == 32 for token in tokens)
    assert len(set(tokens)) == 3
    lifetimes = [await cache_replay.redis.ttl(op.key) for op in versions]
    assert all(0 < lifetime <= 3600 for lifetime in lifetimes)
    assert await cache_replay.redis.get(deleted.key) is None
    assert await cache_replay.rows("SELECT * FROM cache_invalidations") == []
    assert reading(_COUNTERS[0]) - settled == 4
    assert reading("maraclaw_cache_invalidation_last_batch_size") == 4


@pytest.mark.asyncio
async def test_a_pipeline_failure_retries_the_whole_batch_and_does_not_loop_again(
    cache_replay: CacheReplayResources, monkeypatch: pytest.MonkeyPatch, statements: list[str]
) -> None:
    await capture(version_ops(worker.BATCH_SIZE))
    broken = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.1)

    async def unavailable() -> Redis:
        return broken

    statements.clear()
    with monkeypatch.context() as outage:
        outage.setattr(worker, "get_cache_redis", unavailable)
        down = await worker.replay_cache_invalidations()
    await broken.aclose()
    assert down == worker.ReplayPass(claimed=worker.BATCH_SIZE, reached_redis=False)
    assert not down.drain_again
    assert statements == ["WITH", "UPDATE"]
    rows = await cache_replay.rows("SELECT lease_token, attempts FROM cache_invalidations")
    assert rows == [{"lease_token": None, "attempts": 1}] * worker.BATCH_SIZE
    async with cache_replay.pool.connection() as raw:
        await raw.execute("UPDATE cache_invalidations SET available_at = clock_timestamp()")
    assert (await worker.replay_cache_invalidations()).drain_again
    assert await cache_replay.rows("SELECT * FROM cache_invalidations") == []


@pytest.mark.asyncio
async def test_each_retried_row_keeps_its_own_backoff_delay(cache_replay: CacheReplayResources) -> None:
    operations = version_ops(4)
    await capture(operations)
    async with cache_replay.pool.connection() as raw:
        for op, attempts in zip(operations, (0, 1, 2, 8), strict=True):
            await raw.execute("UPDATE cache_invalidations SET attempts = %s WHERE cache_key = %s", (attempts, op.key))
    assert await outbox.retry_batch(await outbox.claim_batch()) == 4
    rows = await cache_replay.rows(
        """SELECT cache_key, lease_token, ceil(extract(epoch FROM available_at - clock_timestamp()))::int AS delay
           FROM cache_invalidations"""
    )
    assert {row["cache_key"]: row["delay"] for row in rows} == dict(
        zip((op.key for op in operations), (2, 4, 8, 60), strict=True)
    )
    assert all(row["lease_token"] is None for row in rows)


@pytest.mark.asyncio
async def test_a_row_captured_again_mid_flight_is_fenced_on_settle_and_retry(
    cache_replay: CacheReplayResources,
) -> None:
    operations = version_ops(3)
    await capture(operations)
    claims = await outbox.claim_batch()
    await capture([operations[1]])
    assert await outbox.settle_batch(claims) == 2
    assert await outbox.retry_batch(claims) == 0
    [fresh] = await outbox.claim_batch()
    stale = next(claim for claim in claims if claim.key == fresh.key)
    assert (fresh.key, fresh.attempts) == (operations[1].key, 1)
    assert fresh.revision != stale.revision
    assert await outbox.settle_batch([fresh]) == 1
    assert await cache_replay.rows("SELECT * FROM cache_invalidations") == []


@pytest.mark.asyncio
async def test_a_pass_counts_settled_retried_and_fenced_rows(
    cache_replay: CacheReplayResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    published, failing, recaptured = version_ops(3)
    await capture([published, failing, recaptured])

    async def publish(claims: Sequence[CacheInvalidationClaim]) -> list[bool]:
        await capture([recaptured])
        return [claim.key != failing.key for claim in claims]

    before = [reading(name) for name in _COUNTERS]
    with monkeypatch.context() as pipeline:
        pipeline.setattr(worker, "publish_batch", publish)
        assert await worker.replay_cache_invalidations() == worker.ReplayPass(claimed=3, reached_redis=True)
    assert [reading(name) - was for name, was in zip(_COUNTERS, before, strict=True)] == [1, 1, 1]
    rows = {
        row["cache_key"]: row
        for row in await cache_replay.rows(
            "SELECT cache_key, attempts, lease_token, available_at > clock_timestamp() AS delayed FROM cache_invalidations"
        )
    }
    assert {key: (row["attempts"], row["lease_token"], row["delayed"]) for key, row in rows.items()} == {
        failing.key: (1, None, True),
        recaptured.key: (0, None, False),
    }
    assert (await worker.replay_cache_invalidations()).claimed == 1
    assert [row["cache_key"] for row in await cache_replay.rows("SELECT cache_key FROM cache_invalidations")] == [
        failing.key
    ]


@pytest.mark.asyncio
async def test_competing_claims_new_producer_and_expired_lease_fence_all_settlement(
    cache_replay: CacheReplayResources,
) -> None:
    key = f"version:{uuid4()}"
    async with session.connection_ctx():
        await redis_cache.bump_version(key)
    competing = await asyncio.gather(outbox.claim_batch(), outbox.claim_batch())
    assert sorted(map(len, competing)) == [0, 1]
    old = next(batch[0] for batch in competing if batch)
    assert old.kind is CacheInvalidationKind.VERSION
    async with session.connection_ctx():
        await redis_cache.bump_version(key)
    assert await outbox.settle_batch([old]) == 0
    assert await outbox.retry_batch([old]) == 0
    fresh = (await outbox.claim_batch())[0]
    assert fresh.revision != old.revision
    async with cache_replay.pool.connection() as raw:
        await raw.execute("UPDATE cache_invalidations SET lease_expires_at = clock_timestamp() - interval '1 second'")
    reclaimed = (await outbox.claim_batch())[0]
    assert reclaimed.lease_token != fresh.lease_token
    assert await outbox.settle_batch([fresh]) == 0
    assert await outbox.retry_batch([fresh]) == 0
    assert await worker.publish_batch([reclaimed]) == [True]
    current = await cache_replay.redis.get(key)
    assert await worker.publish_batch([old]) == [True]
    assert await cache_replay.redis.get(key) != current
    assert await outbox.settle_batch([reclaimed]) == 1


@pytest.mark.asyncio
async def test_lost_acknowledgement_and_expired_acl_token_cannot_resurrect_stale_fill(
    cache_replay: CacheReplayResources,
) -> None:
    agent_id = uuid4()
    user = UserRecord(id=uuid4(), role=UserRole.MEMBER)
    observed = await access_cache.read_acl_snapshot(user.id, agent_id)
    async with session.connection_ctx():
        await access_cache.drop_agent_acl_version(agent_id)
    claim = (await outbox.claim_batch())[0]
    assert await worker.publish_batch([claim]) == [True]
    first = await cache_replay.redis.get(claim.key)
    await access_cache.set_cached_level(user, agent_id, PermissionAccessLevel.MANAGE, observed_ver=observed.version)
    assert await access_cache.get_cached_level(user, agent_id) is None
    assert await worker.publish_batch([claim]) == [True]
    assert await cache_replay.redis.get(claim.key) != first
    await cache_replay.redis.delete(claim.key)
    await cache_replay.redis.set(f"acl:v1:{agent_id}:{user.id}", '{"ver":"0","level":"manage"}')
    assert await access_cache.get_cached_level(user, agent_id) is None
    assert await outbox.settle_batch([claim]) == 1


@pytest.mark.asyncio
async def test_context_replay_rejects_old_fill_and_unversioned_payload(
    cache_replay: CacheReplayResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = uuid4()
    _, observed = await agent_context_cache.read_cached_text(agent_id, "soul")
    monkeypatch.setattr(session, "_flush_cache_invalidations", AsyncMock())
    async with session.connection_ctx():
        await agent_context_cache.invalidate_agent_context(agent_id, "soul")
        assert await agent_context_cache.read_cached_text(agent_id, "soul") == (None, "")
    assert (await worker.replay_cache_invalidations()).claimed == 2
    await agent_context_cache.set_cached_text(agent_id, "soul", "obsolete", observed_ver=observed)
    assert await agent_context_cache.get_cached_text(agent_id, "soul") is None
    await cache_replay.redis.set(f"ctx:{agent_id}:soul", "unversioned old text")
    assert await agent_context_cache.get_cached_text(agent_id, "soul") is None


@pytest.mark.asyncio
async def test_batch_is_bounded_and_cancellation_leaves_lease_recoverable_work(
    cache_replay: CacheReplayResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    await capture(version_ops(53))
    publishing = asyncio.Event()

    async def stalled(_claims: Sequence[CacheInvalidationClaim]) -> list[bool]:
        assert session._conn_ctx.get() is None
        assert (
            await cache_replay.rows(
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle in transaction'"
            )
            == []
        )
        publishing.set()
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(worker, "publish_batch", stalled)
    task = asyncio.create_task(worker.replay_cache_invalidations())
    await publishing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await cache_replay.rows("SELECT lease_token FROM cache_invalidations")
    assert len(rows) == 53
    assert sum(row["lease_token"] is not None for row in rows) == 50
