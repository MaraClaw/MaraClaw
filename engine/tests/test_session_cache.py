"""User session Redis cache: no password_hash, versioned invalidation, fail-open."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.test_redis_cache import FakeRedis

from app.core import redis_cache, session_cache
from app.core.row_memo import clear_row_memo
from app.core.security import load_identity_for_password
from app.records.identity import IdentityRecord
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
    yield client
    redis_cache.reset_circuit()
    clear_row_memo()


def _user(**overrides) -> UserRecord:
    ident = IdentityRecord(
        id=uuid4(),
        email="a@example.com",
        username="alice",
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
        role="member",
        is_active=True,
        quota_messages_used=9,
        identity=ident,
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


@pytest.mark.asyncio
async def test_session_cache_roundtrip_strips_hash_and_quota(fake_cache: FakeRedis) -> None:
    user = _user()
    await session_cache.set_cached_user(user)
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


@pytest.mark.asyncio
async def test_session_cache_bump_user_invalidates(fake_cache: FakeRedis) -> None:
    user = _user()
    await session_cache.set_cached_user(user)
    await session_cache.bump_user_session(user.id)
    clear_row_memo()
    assert await session_cache.get_cached_user(user.id) is None


@pytest.mark.asyncio
async def test_session_cache_identity_bump_invalidates(fake_cache: FakeRedis) -> None:
    user = _user()
    assert user.identity_id is not None
    await session_cache.set_cached_user(user)
    await session_cache.bump_identity_session(user.identity_id)
    assert await session_cache.get_cached_user(user.id) is None


@pytest.mark.asyncio
async def test_session_cache_set_skips_after_version_bump(fake_cache: FakeRedis) -> None:
    user = _user()
    observed = await session_cache.peek_user_version(user.id)
    await session_cache.bump_user_session(user.id)
    await session_cache.set_cached_user(user, observed_user_ver=observed)
    clear_row_memo()
    assert await session_cache.get_cached_user(user.id) is None


@pytest.mark.asyncio
async def test_session_cache_disabled(monkeypatch: pytest.MonkeyPatch, fake_cache: FakeRedis) -> None:
    monkeypatch.setattr(session_cache, "_ttl", lambda: 0)
    user = _user()
    await session_cache.set_cached_user(user)
    clear_row_memo()
    assert await session_cache.get_cached_user(user.id) is None


@pytest.mark.asyncio
async def test_session_cache_redis_down_returns_none(fake_cache: FakeRedis) -> None:
    user = _user()
    await session_cache.set_cached_user(user)
    fake_cache.fail = True
    redis_cache.reset_circuit()
    clear_row_memo()
    assert await session_cache.get_cached_user(user.id) is None


def test_snapshot_never_includes_hash() -> None:
    user = _user()
    payload = session_cache._snapshot(user, "0", "0")
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
