import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core import access_cache, redis_cache
from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.users import UserRole
from app.dao.cache_invalidation_dao import cache_invalidation_dao as outbox
from app.db import session
from app.db.connection import DbConnection
from app.records.cache_invalidation import CacheInvalidation
from app.records.user import UserRecord
from app.services import cache_invalidation_worker as worker
from cache_invalidation_fixtures import CacheReplayResources

pytest_plugins = ("cache_invalidation_fixtures",)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["body", "capture"])
async def test_cancelled_explicit_connection_cannot_later_commit_an_orphaned_mutation(
    cache_replay: CacheReplayResources, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    key = f"version:{uuid4()}"
    capture = outbox.capture

    async def interrupted_capture(db: DbConnection, operations: tuple[CacheInvalidation, ...]) -> None:
        await capture(db, operations)
        raise asyncio.CancelledError

    if stage == "capture":
        monkeypatch.setattr(outbox, "capture", interrupted_capture)
    async with cache_replay.pool.connection() as raw:
        await raw.execute("CREATE TABLE cache_mutation_probe (value text NOT NULL)")
        await raw.commit()

        async def mutate() -> None:
            async with session.transaction(DbConnection(raw)) as db:
                await db.execute("INSERT INTO cache_mutation_probe (value) VALUES (%(value)s)", {"value": key})
                await redis_cache.bump_version(key)
                if stage == "body":
                    raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await mutate()
        await raw.commit()
    assert await cache_replay.rows("SELECT * FROM cache_mutation_probe") == []
    assert await cache_replay.rows("SELECT * FROM cache_invalidations") == []
    assert await cache_replay.redis.get(key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["pooled", "explicit", "durable"])
async def test_commit_then_crash_is_recovered_without_another_mutation(
    cache_replay: CacheReplayResources,
    monkeypatch: pytest.MonkeyPatch,
    ownership: str,
) -> None:
    agent_id, user_id = uuid4(), uuid4()
    version_key, delete_key = f"snapshot:{uuid4()}", f"snapshot:{uuid4()}"
    user = UserRecord(id=user_id, role=UserRole.MEMBER)
    observed = await access_cache.read_acl_snapshot(user_id, agent_id)
    await access_cache.set_cached_level(user, agent_id, PermissionAccessLevel.MANAGE, observed_ver=observed.version)
    await cache_replay.redis.set(delete_key, "old")
    monkeypatch.setattr(session, "_flush_cache_invalidations", AsyncMock(side_effect=asyncio.CancelledError))

    async def mutate() -> None:
        async with session.connection_ctx():
            await access_cache.bump_agent_acl_version(agent_id)
            await access_cache.drop_agent_acl_version(agent_id)
            await redis_cache.bump_version(version_key)
            await redis_cache.cache_delete(delete_key, defer_until_commit=True)

    async def run_mutation() -> None:
        if ownership == "pooled":
            await mutate()
        elif ownership == "durable":
            async with session.durable_transaction():
                await mutate()
        else:
            async with cache_replay.pool.connection() as raw, session.transaction(DbConnection(raw)):
                await mutate()

    with pytest.raises(asyncio.CancelledError):
        await run_mutation()
    rows = await cache_replay.rows("SELECT cache_key, kind FROM cache_invalidations")
    assert {row["kind"] for row in rows} == {"acl_drop", "version", "delete"}
    assert await cache_replay.redis.get(delete_key) == "old"

    monkeypatch.setattr(session, "_flush_cache_invalidations", AsyncMock())
    assert (await worker.replay_cache_invalidations()).claimed == 3
    assert await cache_replay.redis.get(delete_key) is None
    assert bool(await cache_replay.redis.get(version_key))
    assert await access_cache.get_cached_level(user, agent_id) is None
    assert await cache_replay.rows("SELECT * FROM cache_invalidations") == []


@pytest.mark.asyncio
async def test_rollback_and_explicit_flush_keep_independent_committed_batches(
    cache_replay: CacheReplayResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed, rolled_back = f"version:{uuid4()}", f"version:{uuid4()}"
    real_flush = session._flush_cache_invalidations

    async def skip_publication() -> None:
        with monkeypatch.context() as publication:
            publication.setattr(access_cache, "_invalidate_now", AsyncMock())
            publication.setattr(redis_cache, "_client", AsyncMock(side_effect=ConnectionError))
            await real_flush()

    monkeypatch.setattr(session, "_flush_cache_invalidations", skip_publication)

    async def mutate_then_rollback() -> None:
        async with session.connection_ctx():
            await redis_cache.bump_version(committed)
            await session.flush_request_transaction()
            async with session.connection_ctx():
                await redis_cache.bump_version(rolled_back)
            raise ValueError("rollback")

    with pytest.raises(ValueError, match="rollback"):
        await mutate_then_rollback()
    assert [row["cache_key"] for row in await cache_replay.rows("SELECT cache_key FROM cache_invalidations")] == [
        committed
    ]
    assert (await worker.replay_cache_invalidations()).claimed == 1
    assert bool(await cache_replay.redis.get(committed))
    assert await cache_replay.redis.get(rolled_back) is None
