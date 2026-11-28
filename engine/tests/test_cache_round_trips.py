"""Redis round trips of the session and tenant snapshot caches, and what a fill that lost a race leaves behind."""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.test_redis_cache import FakeRedis
from tests.test_session_cache import _user

from app.core import redis_cache, session_cache, tenant_cache
from app.core.row_memo import clear_row_memo
from app.dao.user_dao import user_dao
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord


@pytest.fixture
def cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRedis]:
    redis_cache.reset_circuit()
    clear_row_memo()
    client = FakeRedis()

    async def get_cache_redis() -> FakeRedis:
        return client

    monkeypatch.setattr(redis_cache, "_client", get_cache_redis)
    monkeypatch.setattr(session_cache, "_ttl", lambda: 20)
    monkeypatch.setattr(tenant_cache, "_ttl", lambda: 60)
    session_cache._identity_hints.clear()
    yield client
    session_cache._identity_hints.clear()
    redis_cache.reset_circuit()
    clear_row_memo()


class Checkouts:
    """A stand-in for the user DAO's pool checkouts that records the Redis trips made while each one was held."""

    def __init__(self, cache: FakeRedis, user: UserRecord) -> None:
        self.cache = cache
        self.user = user
        self.held: list[int] = []

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[Checkouts]:
        opened = self.cache.round_trips
        yield self
        self.held.append(self.cache.round_trips - opened)

    async def fetchone(self, _query: str, _params: object) -> dict[str, object]:
        return {}

    async def fetchval(self, _query: str, _params: object) -> object:
        return self.user.identity_id


@pytest.fixture
def checkouts(cache: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> Checkouts:
    user = _user()
    held = Checkouts(cache, user)
    monkeypatch.setattr(user_dao, "session", held.session)
    monkeypatch.setattr(sys.modules["app.dao.user_dao"], "_user_from_joined_row", lambda _row: user)
    return held


async def test_a_warm_session_miss_costs_two_round_trips_and_none_while_a_connection_is_held(
    cache: FakeRedis, checkouts: Checkouts
) -> None:
    user = checkouts.user
    assert await user_dao.get_with_identity(user.id) is user
    await session_cache.bump_user_session(user.id)
    clear_row_memo()
    cache.round_trips = 0
    checkouts.held.clear()

    assert await user_dao.get_with_identity(user.id) is user

    assert cache.round_trips == 2  # the lookup, then the fenced fill
    assert checkouts.held == [0]  # one checkout, for the row, with no Redis call inside it
    clear_row_memo()
    cache.round_trips = 0
    loaded = await user_dao.get_with_identity(user.id)
    assert loaded is not None
    assert loaded.id == user.id
    assert cache.round_trips == 1
    assert checkouts.held == [0]


@pytest.mark.parametrize("hinted", [False, True])
async def test_initializing_both_generations_and_filling_still_use_two_trips(
    cache: FakeRedis, checkouts: Checkouts, hinted: bool
) -> None:
    user = checkouts.user
    assert user.identity_id is not None
    if hinted:
        session_cache._identity_hints[user.id] = user.identity_id

    assert await user_dao.get_with_identity(user.id) is user

    assert cache.round_trips == 2
    assert checkouts.held == ([0] if hinted else [0, 0])
    assert session_cache._identity_hints == {user.id: user.identity_id}
    assert session_cache._sess_key(user.id) in cache.store


async def test_a_session_fill_whose_generation_moved_before_it_landed_leaves_no_snapshot(cache: FakeRedis) -> None:
    user = _user()
    lookup = await session_cache.lookup_cached_user(user.id, find_identity=AsyncMock(return_value=user.identity_id))
    observed = lookup.observation
    assert observed is not None
    await session_cache.bump_user_session(user.id)

    await session_cache.set_cached_user(user, observed=observed)

    clear_row_memo()  # the request that loaded the row keeps it; no later request may be served it
    assert await session_cache.get_cached_user(user.id) is None
    assert session_cache._sess_key(user.id) not in cache.store


def _tenant() -> TenantRecord:
    return TenantRecord(id=uuid4(), name="Acme", slug="acme", tier=OrganizationPlanId.PRO)


async def test_a_tenant_hit_and_a_tenant_fill_are_one_round_trip_each(cache: FakeRedis) -> None:
    tenant = _tenant()
    observed = await tenant_cache.peek_tenant_version(tenant.id)
    cache.round_trips = 0

    await tenant_cache.set_cached_tenant(tenant, observed_ver=observed)
    assert cache.round_trips == 1
    hit = await tenant_cache.get_cached_tenant(tenant.id)

    assert hit is not None
    assert hit.id == tenant.id
    assert cache.round_trips == 2


async def test_a_tenant_fill_whose_generation_moved_leaves_no_snapshot(cache: FakeRedis) -> None:
    tenant = _tenant()
    observed = await tenant_cache.peek_tenant_version(tenant.id)
    await tenant_cache.bump_tenant_cache(tenant.id)

    await tenant_cache.set_cached_tenant(tenant, observed_ver=observed)

    assert await tenant_cache.get_cached_tenant(tenant.id) is None
    assert tenant_cache._row_key(tenant.id) not in cache.store


async def test_read_keys_and_ensure_versions_are_one_round_trip_whatever_the_key_count(cache: FakeRedis) -> None:
    cache.store["b"] = "kept"

    assert await redis_cache.read_keys(["a", "b", "c"]) == [None, "kept", None]
    assert cache.round_trips == 1
    generations = await redis_cache.read_keys(["b", "v1", "v2"], initialize=["b", "v1", "v2"], generation_ttl=20)

    assert cache.round_trips == 2
    assert generations is not None
    assert generations[0] == "kept"
    assert cache.store["v1"] == generations[1]
    assert cache.expires["v1"] >= 3600


async def test_a_fill_commits_only_while_every_fence_still_reads_what_was_observed(cache: FakeRedis) -> None:
    cache.store["gen"] = "g1"
    cache.round_trips = 0

    assert await redis_cache.cache_fill_json("snap", {"a": 1}, ttl=30, fences=[("gen", "g1")])
    assert cache.round_trips == 1
    assert cache.store["snap"] == '{"a":1}'
    assert not await redis_cache.cache_fill_json("snap2", {"a": 1}, ttl=30, fences=[("gen", "stale")])
    assert "snap2" not in cache.store
    cache.fail = True
    assert not await redis_cache.cache_fill_json("snap3", {"a": 1}, ttl=30, fences=[("gen", "g1")])
