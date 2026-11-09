"""Isolated PostgreSQL schemas and opt-in real Redis for cache replay contracts."""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from psycopg import AsyncConnection, sql
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from app.core import access_cache, redis_cache, row_memo, session_cache, tenant_cache
from app.db import session
from app.db.connection import DbConnection
from app.services import agent_context_cache, cache_invalidation_worker

BASELINE_SQL = (Path(__file__).resolve().parents[1] / "scripts/schema_baseline.sql").read_bytes()


@dataclass(frozen=True, slots=True)
class CacheReplayResources:
    pool: AsyncConnectionPool[AsyncConnection[DictRow]]
    redis: Redis

    async def rows(self, query: str) -> list[dict[str, object]]:
        async with self.pool.connection() as raw:
            return await DbConnection(raw).fetchall(query)


@pytest_asyncio.fixture
async def cache_replay(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CacheReplayResources]:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN", "")
    redis_url = os.environ.get("MARACLAW_TEST_REDIS_URL", "")
    if not dsn or not redis_url:
        pytest.skip("MARACLAW_TEST_POSTGRES_DSN and MARACLAW_TEST_REDIS_URL required for real cache recovery")
    schema = f"cache_replay_{uuid4().hex}"
    admin = await AsyncConnection.connect(dsn, autocommit=True)
    client = Redis.from_url(redis_url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1)
    await client.ping()
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    async with AsyncConnectionPool[AsyncConnection[DictRow]](
        dsn,
        min_size=1,
        max_size=8,
        open=False,
        kwargs={"row_factory": dict_row, "options": f"-csearch_path={schema}"},
    ) as pool:
        await pool.wait()
        async with pool.connection() as raw:
            await raw.execute(BASELINE_SQL)
        monkeypatch.setattr(session, "get_pool", lambda: pool)
        monkeypatch.setattr(session, "durable_pool", AsyncMock(return_value=pool))
        for module, name in (
            (access_cache, "get_cache_redis"),
            (redis_cache, "_client"),
            (agent_context_cache, "get_redis"),
            (cache_invalidation_worker, "get_cache_redis"),
        ):

            async def get_client() -> Redis:
                return client

            monkeypatch.setattr(module, name, get_client)
        monkeypatch.setattr(access_cache, "_ttl_seconds", lambda: 45)
        for module in (session_cache, tenant_cache, agent_context_cache):
            monkeypatch.setattr(module, "_ttl", lambda: 60)
        redis_cache.reset_circuit()
        token = session._conn_ctx.set(None)
        try:
            yield CacheReplayResources(pool, client)
        finally:
            session._conn_ctx.reset(token)
            access_cache.clear_request_memo()
            redis_cache.reset_circuit()
            row_memo.clear_row_memo()
    await client.aclose()
    await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    await admin.close()
