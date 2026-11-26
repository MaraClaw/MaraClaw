"""User session Redis cache: no password_hash, versioned invalidation, fail-open."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.test_db_session import _FakePool, _FakeRawConnection
from tests.test_redis_cache import FakeRedis

from app.core import redis_cache, session_cache, tenant_cache
from app.core.cache_contracts import SessionCacheObservation
from app.core.domain.users import UserRole
from app.core.json_types import is_json_object
from app.core.row_memo import clear_row_memo
from app.core.security import load_identity_for_password
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db import session as db_session
from app.records.identity import IdentityRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch):
    redis_cache.reset_circuit()
    clear_row_memo()
    client = FakeRedis()

    async def get_cache_redis() -> FakeRedis:
        return client

    monkeypatch.setattr(redis_cache, "_client", get_cache_redis)
    monkeypatch.setattr(session_cache, "_ttl", lambda: 20)
    session_cache._identity_hints.clear()
    yield client
    session_cache._identity_hints.clear()
    redis_cache.reset_circuit()
    clear_row_memo()


def _user(**overrides) -> UserRecord:
    ident = IdentityRecord(
        id=uuid4(),
        email="a@example.com",
        password_hash="secret-hash",
        is_active=True,
        is_platform_admin=False,
        email_verified=True,
        must_change_password=False,
    )
    user = UserRecord(
        id=uuid4(),
        identity_id=ident.id,
        tenant_id=uuid4(),
        display_name="Alice",
        role=UserRole.MEMBER,
        is_active=True,
        quota_messages_used=9,
        identity=ident,
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


async def _observation(user: UserRecord) -> SessionCacheObservation | None:
    user_ver = await session_cache.peek_user_version(user.id)
    identity_ver = await session_cache.peek_identity_version(user.identity_id)
    if user_ver is None or identity_ver is None:
        return None
    return SessionCacheObservation(user.identity_id, user_ver, identity_ver)


@pytest.mark.asyncio
async def test_session_cache_roundtrip_strips_hash_and_quota(fake_cache: FakeRedis) -> None:
    user = _user()
    await session_cache.set_cached_user(user, observed=await _observation(user))
    clear_row_memo()
    loaded = await session_cache.get_cached_user(user.id)
    assert loaded is not None
    assert loaded.email == "a@example.com"
    assert loaded.identity is not None
    assert loaded.identity.password_hash is None
    assert loaded.quota_messages_used == 0
    dumped = str(fake_cache.store)
    assert "secret-hash" not in dumped
    assert "password_hash" not in dumped


async def test_a_session_hit_reads_the_snapshot_and_both_generations_in_one_round_trip(fake_cache: FakeRedis) -> None:
    # Given a warm snapshot and no request memo.
    user = _user()
    assert user.identity_id is not None
    await session_cache.set_cached_user(user, observed=await _observation(user))
    clear_row_memo()
    fake_cache.commands.clear()

    # When the next request reads it.
    loaded = await session_cache.get_cached_user(user.id)

    # Then one pipeline of single-key GETs, each key named explicitly, covered the snapshot and both generations.
    assert loaded is not None
    assert loaded.id == user.id
    assert fake_cache.commands == ["pipeline"]
    assert fake_cache.round_trips == 1
    assert fake_cache.pipelines[-1] == [
        ("get", (session_cache._sess_key(user.id),), None),
        ("get", (session_cache.user_version_key(user.id),), None),
        ("get", (session_cache.identity_version_key(user.identity_id),), None),
    ]


async def test_the_first_read_of_a_membership_takes_two_round_trips_and_every_later_one_takes_one(
    fake_cache: FakeRedis,
) -> None:
    # Given a warm snapshot in a process that has not served this membership yet.
    user = _user()
    await session_cache.set_cached_user(user, observed=await _observation(user))
    session_cache._identity_hints.clear()
    clear_row_memo()
    fake_cache.commands.clear()

    # When the membership is read twice.
    first = await session_cache.get_cached_user(user.id)
    clear_row_memo()
    after_first = list(fake_cache.commands)
    second = await session_cache.get_cached_user(user.id)

    # Then only the first read pays a second trip, for the identity generation the snapshot names.
    assert first is not None
    assert second is not None
    assert after_first == ["pipeline", "get"]
    assert fake_cache.commands == ["pipeline", "get", "pipeline"]
    assert [len(ops) for ops in fake_cache.pipelines[-2:]] == [2, 3]


@pytest.mark.parametrize("filled", [True, False])
async def test_a_membership_loaded_from_sql_teaches_the_identity_hint_whether_or_not_it_is_cached(
    fake_cache: FakeRedis, filled: bool
) -> None:
    user = _user()

    await session_cache.set_cached_user(user, observed=await _observation(user) if filled else None)

    assert session_cache._identity_hints == {user.id: user.identity_id}


async def test_a_hint_naming_another_identity_is_a_miss_and_is_corrected_for_the_next_read(
    fake_cache: FakeRedis,
) -> None:
    # Given a warm snapshot, and a hint naming an identity whose generation happens to equal the snapshot's.
    user = _user()
    observed = await _observation(user)
    assert observed is not None
    await session_cache.set_cached_user(user, observed=observed)
    wrong = uuid4()
    fake_cache.store[session_cache.identity_version_key(wrong)] = observed.identity_version
    session_cache._identity_hints[user.id] = wrong
    clear_row_memo()

    # When the membership is read.
    assert await session_cache.get_cached_user(user.id) is None

    # Then the wrong key's matching generation vouched for nothing, and the next read hits with the corrected hint.
    assert session_cache._identity_hints[user.id] == user.identity_id
    fake_cache.commands.clear()
    loaded = await session_cache.get_cached_user(user.id)
    assert loaded is not None
    assert loaded.id == user.id
    assert fake_cache.commands == ["pipeline"]


@pytest.mark.parametrize("hinted", [True, False])
@pytest.mark.parametrize("moved", ["user", "identity"])
async def test_a_moved_generation_refuses_the_snapshot_whether_or_not_the_identity_is_hinted(
    fake_cache: FakeRedis, moved: str, hinted: bool
) -> None:
    # Given a warm snapshot, read through the hint or by naming the identity from the snapshot.
    user = _user()
    assert user.identity_id is not None
    await session_cache.set_cached_user(user, observed=await _observation(user))
    if not hinted:
        session_cache._identity_hints.clear()
    clear_row_memo()

    # When one generation moves while the snapshot itself stays in Redis.
    await redis_cache.bump_version(
        session_cache.user_version_key(user.id)
        if moved == "user"
        else session_cache.identity_version_key(user.identity_id)
    )

    # Then the generation alone refuses the snapshot.
    assert session_cache._sess_key(user.id) in fake_cache.store
    assert await session_cache.get_cached_user(user.id) is None


async def test_a_membership_without_an_identity_hits_in_one_round_trip_and_forgets_a_stale_hint(
    fake_cache: FakeRedis,
) -> None:
    # Given a warm snapshot of a membership with no identity, and a hint left over from an earlier identity.
    user = _user(identity_id=None, identity=None)
    await session_cache.set_cached_user(user, observed=await _observation(user))
    session_cache._identity_hints[user.id] = uuid4()
    clear_row_memo()
    fake_cache.commands.clear()

    # When it is read.
    loaded = await session_cache.get_cached_user(user.id)

    # Then the snapshot hits without any identity generation, and the stale hint is gone.
    assert loaded is not None
    assert fake_cache.commands == ["pipeline"]
    assert user.id not in session_cache._identity_hints


async def test_identity_hints_are_bounded_and_evict_the_least_recently_used(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given room for two hints, two filled memberships, and a hit that makes the first the most recently used.
    monkeypatch.setattr(session_cache, "_IDENTITY_HINTS_MAX", 2)
    first, second, third = _user(), _user(), _user()
    for user in (first, second):
        await session_cache.set_cached_user(user, observed=await _observation(user))
    clear_row_memo()
    assert await session_cache.get_cached_user(first.id) is not None

    # When a third membership is loaded.
    await session_cache.set_cached_user(third, observed=await _observation(third))

    # Then the one nobody touched is forgotten.
    assert list(session_cache._identity_hints) == [first.id, third.id]


async def test_a_session_hit_through_the_dao_costs_one_round_trip_and_no_sql(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a warm snapshot and a DAO that fails if it reaches SQL.
    user = _user()
    await session_cache.set_cached_user(user, observed=await _observation(user))
    clear_row_memo()
    fake_cache.commands.clear()
    monkeypatch.setattr(user_dao, "_load_with_identity", AsyncMock(side_effect=AssertionError("reached SQL")))

    # When the principal is loaded for a request.
    loaded = await user_dao.get_with_identity(user.id)

    # Then the cache answered in a single call.
    assert loaded is not None
    assert fake_cache.commands == ["pipeline"]


async def test_an_unavailable_cache_falls_open_to_sql_after_one_attempt(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a Redis that fails every call and a DAO load that stands in for SQL.
    user = _user()
    fake_cache.fail = True
    load = AsyncMock(return_value=user)
    monkeypatch.setattr(user_dao, "_load_with_identity", load)

    # When two requests load the principal.
    first = await user_dao.get_with_identity(user.id)
    clear_row_memo()
    second = await user_dao.get_with_identity(user.id)

    # Then both are answered from SQL, and only the first paid for a Redis attempt before the breaker opened.
    assert (first, second) == (user, user)
    assert load.await_count == 2
    assert fake_cache.commands == ["pipeline"]


@pytest.mark.parametrize("hinted", [True, False])
async def test_an_unavailable_cache_reads_as_a_miss_after_one_attempt(fake_cache: FakeRedis, hinted: bool) -> None:
    # Given a warm snapshot, then a Redis that fails every call.
    user = _user()
    await session_cache.set_cached_user(user, observed=await _observation(user))
    if not hinted:
        session_cache._identity_hints.clear()
    fake_cache.fail = True
    clear_row_memo()
    fake_cache.commands.clear()

    # When the membership is read twice.
    first = await session_cache.get_cached_user(user.id)
    second = await session_cache.get_cached_user(user.id)

    # Then both reads fall open to SQL and only the first reached Redis.
    assert (first, second) == (None, None)
    assert fake_cache.commands == ["pipeline"]


@pytest.mark.parametrize("stored", ["not json", "[]", "5", '"text"', '{"user": 5}', '{"user": {}}'])
async def test_a_malformed_snapshot_is_a_miss_after_one_round_trip(fake_cache: FakeRedis, stored: str) -> None:
    user = _user()
    fake_cache.store[session_cache._sess_key(user.id)] = stored
    fake_cache.commands.clear()

    assert await session_cache.get_cached_user(user.id) is None
    assert fake_cache.commands == ["pipeline"]


@pytest.mark.parametrize("timezone", ["UTC", "Etc/GMT-7", "Asia/Shanghai", None])
async def test_session_cache_roundtrip_preserves_timezone(fake_cache: FakeRedis, timezone: str | None) -> None:
    # Given a member preference, including an explicitly unset preference.
    user = _user(timezone=timezone)
    await session_cache.set_cached_user(user, observed=await _observation(user))
    clear_row_memo()
    # When another request reads the Redis snapshot rather than the row memo.
    loaded = await session_cache.get_cached_user(user.id)
    # Then the preference survives serialization and deserialization unchanged.
    assert loaded is not None
    assert loaded.timezone == timezone


async def test_legacy_session_snapshot_defaults_timezone_to_unset(fake_cache: FakeRedis) -> None:
    # Given a pre-preference snapshot with no timezone key.
    user = _user()
    observed = await _observation(user)
    assert observed is not None
    payload = dict[str, object](session_cache._snapshot(user, observed.user_version, observed.identity_version))
    user_payload = payload["user"]
    assert is_json_object(user_payload)
    user_payload.pop("timezone", None)
    await redis_cache.cache_set_json(session_cache._sess_key(user.id), payload, ttl=20)
    # When the old snapshot is read after deployment.
    loaded = await session_cache.get_cached_user(user.id)
    # Then it remains readable with an unset preference.
    assert loaded is not None
    assert loaded.timezone is None


@pytest.mark.asyncio
async def test_legacy_snapshot_ignores_retired_field_without_invalidating_session(fake_cache: FakeRedis) -> None:
    user = _user()
    assert user.identity is not None
    user.identity.must_change_password = True
    observed = await _observation(user)
    assert observed is not None
    payload = dict[str, object](session_cache._snapshot(user, observed.user_version, observed.identity_version))
    identity_payload = payload["identity"]
    assert is_json_object(identity_payload)
    identity_payload["username"] = "legacy-handle"
    await redis_cache.cache_set_json(session_cache._sess_key(user.id), payload, ttl=20)

    loaded = await session_cache.get_cached_user(user.id)

    assert loaded is not None
    assert (loaded.id, loaded.identity_id, loaded.tenant_id) == (user.id, user.identity_id, user.tenant_id)
    assert loaded.email == "a@example.com"
    assert loaded.must_change_password is True
    assert loaded.identity is not None
    assert loaded.identity.password_hash is None
    assert not hasattr(loaded, "username")
    assert not hasattr(loaded.identity, "username")
    assert "username" not in str(session_cache._snapshot(loaded, "0", "0"))
    assert session_cache._sess_key(user.id) in fake_cache.store


@pytest.mark.asyncio
async def test_session_cache_bump_user_invalidates(fake_cache: FakeRedis) -> None:
    user = _user()
    await session_cache.set_cached_user(user, observed=await _observation(user))
    await session_cache.bump_user_session(user.id)
    clear_row_memo()
    assert await session_cache.get_cached_user(user.id) is None


@pytest.mark.asyncio
async def test_bump_in_nested_scope_flushes_once_at_the_outer_commit(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an outer scope with a joined inner scope that bumps the same user twice
    raw = _FakeRawConnection()
    monkeypatch.setattr(db_session, "get_pool", lambda: _FakePool(raw))
    user = _user()
    version_key = session_cache.user_version_key(user.id)
    sess_key = session_cache._sess_key(user.id)
    fake_cache.store[sess_key] = "snapshot"
    # When
    async with db_session.connection_ctx():
        async with db_session.connection_ctx():
            await session_cache.bump_user_session(user.id)
            async with db_session.transaction():
                await session_cache.bump_user_session(user.id)
        # Then nothing reached Redis before the outer commit
        assert (fake_cache.pipelines, raw.commits) == ([], 0)
        assert sess_key in fake_cache.store
    # And exactly one pipeline rotates the version and drops the snapshot afterwards
    assert raw.commits == 1
    assert len(fake_cache.pipelines) == 1
    assert [(op, args[0]) for op, args, _ in fake_cache.pipelines[0]] == [("set", version_key), ("delete", sess_key)]
    assert version_key in fake_cache.store
    assert sess_key not in fake_cache.store


@pytest.mark.asyncio
async def test_session_cache_identity_bump_invalidates(fake_cache: FakeRedis) -> None:
    user = _user()
    assert user.identity_id is not None
    await session_cache.set_cached_user(user, observed=await _observation(user))
    await session_cache.bump_identity_session(user.identity_id)
    assert await session_cache.get_cached_user(user.id) is None


@pytest.mark.asyncio
async def test_session_cache_set_skips_after_version_bump(fake_cache: FakeRedis) -> None:
    user = _user()
    observed = await _observation(user)
    await session_cache.bump_user_session(user.id)
    await session_cache.set_cached_user(user, observed=observed)
    clear_row_memo()
    assert await session_cache.get_cached_user(user.id) is None


@pytest.mark.asyncio
async def test_session_cache_disabled(monkeypatch: pytest.MonkeyPatch, fake_cache: FakeRedis) -> None:
    monkeypatch.setattr(session_cache, "_ttl", lambda: 0)
    user = _user()
    await session_cache.set_cached_user(user, observed=await _observation(user))
    clear_row_memo()
    assert await session_cache.get_cached_user(user.id) is None


@pytest.mark.asyncio
async def test_session_cache_redis_down_returns_none(fake_cache: FakeRedis) -> None:
    user = _user()
    await session_cache.set_cached_user(user, observed=await _observation(user))
    fake_cache.fail = True
    redis_cache.reset_circuit()
    clear_row_memo()
    assert await session_cache.get_cached_user(user.id) is None


@pytest.mark.parametrize("hinted", [True, False])
@pytest.mark.parametrize("dependency", ["user", "identity"])
async def test_session_misses_when_only_generation_read_fails(
    fake_cache: FakeRedis, dependency: str, hinted: bool
) -> None:
    user = _user()
    assert user.identity_id is not None
    key = (
        session_cache.user_version_key(user.id)
        if dependency == "user"
        else session_cache.identity_version_key(user.identity_id)
    )
    fake_cache.store[key] = "0"
    await session_cache.set_cached_user(user, observed=await _observation(user))
    if not hinted:
        session_cache._identity_hints.clear()
    clear_row_memo()
    fake_cache.fail_gets.add(key)

    loaded = await session_cache.get_cached_user(user.id)

    assert loaded is None


@pytest.mark.parametrize("hinted", [True, False])
@pytest.mark.parametrize("dependency", ["user", "identity"])
@pytest.mark.parametrize("recreate", [False, True])
async def test_expired_generation_cannot_revalidate_old_session(
    fake_cache: FakeRedis, dependency: str, recreate: bool, hinted: bool
) -> None:
    user = _user()
    assert user.identity_id is not None
    key = (
        session_cache.user_version_key(user.id)
        if dependency == "user"
        else session_cache.identity_version_key(user.identity_id)
    )
    await redis_cache.bump_version(key)
    await session_cache.set_cached_user(user, observed=await _observation(user))
    if not hinted:
        session_cache._identity_hints.clear()
    clear_row_memo()
    fake_cache.store.pop(key)

    if recreate:
        await redis_cache.bump_version(key)
    loaded = await session_cache.get_cached_user(user.id)

    assert loaded is None


@pytest.mark.parametrize("after_observation", [False, True])
@pytest.mark.parametrize("dependency", ["user", "identity"])
async def test_session_fill_requires_successful_generation_observations(
    fake_cache: FakeRedis, dependency: str, after_observation: bool
) -> None:
    user = _user()
    assert user.identity_id is not None
    key = (
        session_cache.user_version_key(user.id)
        if dependency == "user"
        else session_cache.identity_version_key(user.identity_id)
    )
    observed = await _observation(user) if after_observation else None
    fake_cache.fail_gets.add(key)
    if not after_observation:
        observed = await _observation(user)
        fake_cache.fail_gets.clear()
        redis_cache.reset_circuit()

    await session_cache.set_cached_user(user, observed=observed)

    assert session_cache._sess_key(user.id) not in fake_cache.store


@pytest.mark.parametrize("dependency", ["user", "identity"])
async def test_session_fill_refuses_missing_current_generation(fake_cache: FakeRedis, dependency: str) -> None:
    user = _user()
    assert user.identity_id is not None
    observed = await _observation(user)
    key = (
        session_cache.user_version_key(user.id)
        if dependency == "user"
        else session_cache.identity_version_key(user.identity_id)
    )
    fake_cache.store.pop(key)

    await session_cache.set_cached_user(user, observed=observed)

    assert session_cache._sess_key(user.id) not in fake_cache.store


def test_snapshot_never_includes_hash() -> None:
    user = _user()
    payload = session_cache._snapshot(user, "0", "0")
    assert payload["identity"] is not None
    assert "password_hash" not in payload["identity"]
    assert payload["identity"]["must_change_password"] is False
    assert payload["user"]["role"] == "member"
    assert "quota_messages_used" not in payload["user"]


@pytest.mark.asyncio
async def test_load_identity_for_password_uses_dao(monkeypatch: pytest.MonkeyPatch) -> None:
    ident = IdentityRecord(id=uuid4(), password_hash="live-hash")

    async def fake_get(item_id: object) -> IdentityRecord | None:
        assert item_id == ident.id
        return ident

    monkeypatch.setattr("app.core.security.identity_dao.get", fake_get)
    loaded = await load_identity_for_password(ident.id)
    assert loaded is not None
    assert loaded.password_hash == "live-hash"


def test_settings_exist() -> None:
    settings = SimpleNamespace(
        USER_SESSION_CACHE_TTL_SECONDS=20,
        TENANT_CACHE_TTL_SECONDS=60,
    )
    assert settings.USER_SESSION_CACHE_TTL_SECONDS == 20


class _HeldLoad:
    """A DAO load held open until released, so a test controls which callers overlap it."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[object] = []
        self.release = asyncio.Event()
        self.failure: Exception | None = None

    async def __call__(self, *args: object) -> object:
        self.calls.append(args)
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return self.result


async def test_concurrent_session_misses_share_one_load(fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    load = _HeldLoad(user)
    monkeypatch.setattr(user_dao, "_load_with_identity", load)

    readers = [asyncio.create_task(user_dao.get_with_identity(user.id)) for _ in range(3)]
    await asyncio.sleep(0)
    load.release.set()

    assert await asyncio.gather(*readers) == [user, user, user]
    assert len(load.calls) == 1


async def test_a_failed_session_load_reaches_every_reader_and_the_next_read_retries(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user()
    load = _HeldLoad(user)
    load.failure = OSError("db down")
    monkeypatch.setattr(user_dao, "_load_with_identity", load)

    readers = [asyncio.create_task(user_dao.get_with_identity(user.id)) for _ in range(2)]
    await asyncio.sleep(0)
    load.release.set()
    outcomes = await asyncio.gather(*readers, return_exceptions=True)

    assert [type(outcome) for outcome in outcomes] == [OSError, OSError]
    load.failure = None
    assert await user_dao.get_with_identity(user.id) is user
    assert len(load.calls) == 2


async def test_a_session_miss_after_a_version_bump_does_not_share_the_older_load(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user()
    load = _HeldLoad(user)
    monkeypatch.setattr(user_dao, "_load_with_identity", load)

    before = asyncio.create_task(user_dao.get_with_identity(user.id))
    await asyncio.sleep(0)
    await session_cache.bump_user_session(user.id)
    after = asyncio.create_task(user_dao.get_with_identity(user.id))
    await asyncio.sleep(0)
    load.release.set()
    await asyncio.gather(before, after)

    assert len(load.calls) == 2


async def test_session_loads_inside_a_transaction_are_never_shared(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _FakeRawConnection()
    monkeypatch.setattr(db_session, "get_pool", lambda: _FakePool(raw))
    user = _user()
    load = _HeldLoad(user)
    monkeypatch.setattr(user_dao, "_load_with_identity", load)

    async with db_session.connection_ctx():
        readers = [asyncio.create_task(user_dao.get_with_identity(user.id)) for _ in range(2)]
        await asyncio.sleep(0)
        load.release.set()
        await asyncio.gather(*readers)

    assert len(load.calls) == 2


async def test_concurrent_tenant_misses_share_one_load(fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme", tier=OrganizationPlanId.PRO)
    load = _HeldLoad(tenant)
    monkeypatch.setattr(tenant_cache, "_ttl", lambda: 60)
    monkeypatch.setattr(tenant_dao, "_load_and_cache", load)

    readers = [asyncio.create_task(tenant_dao.get(tenant.id)) for _ in range(3)]
    await asyncio.sleep(0)
    load.release.set()

    assert await asyncio.gather(*readers) == [tenant, tenant, tenant]
    assert len(load.calls) == 1
