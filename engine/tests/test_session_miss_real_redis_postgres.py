"""A session miss against a real PostgreSQL and a real Redis: what goes on the wire, where the pool is, what a race leaves."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.asyncio.connection import Connection

from app.core import redis_cache, row_memo, session_cache
from app.dao.user_dao import user_dao

if TYPE_CHECKING:
    from tenant_deletion_pg_fixtures import DeletionPostgres

pytest_plugins = ("tenant_deletion_pg_fixtures",)
pytestmark = pytest.mark.parametrize("deletion_pg", ["warm"], indirect=True)


@dataclass(frozen=True, slots=True)
class Wire:
    client: Redis
    writes: list[bytes]
    held: list[int]  # pool connections checked out at each write


def _gets(*keys: str) -> bytes:
    return b"".join(f"*2\r\n$3\r\nGET\r\n${len(key)}\r\n{key}\r\n".encode() for key in keys)


@pytest.fixture
async def wire(monkeypatch: pytest.MonkeyPatch, deletion_pg: DeletionPostgres) -> AsyncIterator[Wire]:
    url = os.environ.get("MARACLAW_TEST_REDIS_URL", "")
    if not url:
        pytest.skip("MARACLAW_TEST_REDIS_URL required for the real session miss")
    async with Redis.from_url(url, decode_responses=True) as client:

        async def real_client() -> Redis:
            return client

        monkeypatch.setattr(redis_cache, "_client", real_client)
        _ = await client.ping()
        seen = Wire(client, [], [])
        send = Connection.send_packed_command

        async def recording(
            self: Connection, command: bytes | str | Iterable[bytes], check_health: bool = True
        ) -> None:
            chunks = (
                [command.encode() if isinstance(command, str) else command]
                if isinstance(command, bytes | str)
                else list(command)
            )
            stats = deletion_pg.pool.get_stats()
            seen.held.append(stats["pool_size"] - stats["pool_available"])
            seen.writes.append(b"".join(chunks))
            await send(self, chunks, check_health=check_health)

        monkeypatch.setattr(Connection, "send_packed_command", recording)
        redis_cache.reset_circuit()
        row_memo.clear_row_memo()
        session_cache._identity_hints.clear()
        yield seen
        assert deletion_pg.actor.identity_id is not None
        _ = await client.delete(
            redis_cache.cache_key("sess", "v1", deletion_pg.actor.id),
            session_cache.user_version_key(deletion_pg.actor.id),
            session_cache.identity_version_key(deletion_pg.actor.identity_id),
        )
        row_memo.clear_row_memo()
        session_cache._identity_hints.clear()
        redis_cache.reset_circuit()


@pytest.mark.parametrize("cache_state", ["empty", "warm-generations"])
async def test_a_session_miss_is_a_read_pipeline_then_a_fenced_fill_with_no_connection_held_at_either(
    deletion_pg: DeletionPostgres, wire: Wire, cache_state: str
) -> None:
    actor = deletion_pg.actor
    assert actor.identity_id is not None
    sess_key = redis_cache.cache_key("sess", "v1", actor.id)
    user_key, identity_key = (
        session_cache.user_version_key(actor.id),
        session_cache.identity_version_key(actor.identity_id),
    )
    if cache_state == "warm-generations":
        assert await user_dao.get_with_identity(actor.id) is not None
        await session_cache.bump_user_session(actor.id)
        row_memo.clear_row_memo()
        wire.writes.clear()
        wire.held.clear()

    loaded = await user_dao.get_with_identity(actor.id)

    assert loaded is not None
    assert len(wire.writes) == 2
    assert wire.writes[0].endswith(_gets(sess_key, user_key, identity_key))
    assert wire.writes[0].count(b"\r\nNX\r\n") == 2
    assert wire.writes[1].startswith(_gets(user_key, identity_key))
    assert b"SET" in wire.writes[1]
    assert wire.held == [0, 0]
    assert await wire.client.get(sess_key) is not None


async def test_a_fill_observed_before_a_revocation_bumped_the_generation_leaves_nothing_in_redis(
    deletion_pg: DeletionPostgres, wire: Wire
) -> None:
    actor = deletion_pg.actor
    lookup = await session_cache.lookup_cached_user(actor.id, find_identity=AsyncMock(return_value=actor.identity_id))
    observed = lookup.observation
    assert observed is not None
    row = await user_dao.get_with_identity(actor.id, fresh=True)
    assert row is not None
    await session_cache.bump_user_session(actor.id)

    await session_cache.set_cached_user(row, observed=observed)

    assert await wire.client.get(redis_cache.cache_key("sess", "v1", actor.id)) is None
    row_memo.clear_row_memo()
    assert await session_cache.get_cached_user(actor.id) is None
