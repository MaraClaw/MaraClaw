"""Tenant row cache, IM token cache, and shared channel dedup."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.test_redis_cache import FakeRedis

from app.core import redis_cache, tenant_cache
from app.core.row_memo import clear_row_memo
from app.records.tenant import TenantRecord
from app.services import im_token_cache
from app.services.channels import dedup as channel_dedup


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch):
    redis_cache.reset_circuit()
    clear_row_memo()
    client = FakeRedis()

    async def get_cache_redis() -> FakeRedis:
        return client

    monkeypatch.setattr(redis_cache, "_client", get_cache_redis)
    monkeypatch.setattr(tenant_cache, "_ttl", lambda: 60)
    monkeypatch.setattr(im_token_cache, "_ttl_enabled", lambda: True)
    monkeypatch.setattr(channel_dedup, "_redis_ttl", lambda: 86400)
    yield client
    redis_cache.reset_circuit()
    clear_row_memo()
    channel_dedup._stores.clear()


def _tenant() -> TenantRecord:
    return TenantRecord(
        id=uuid4(),
        name="Acme",
        slug="acme",
        timezone="Asia/Shanghai",
        a2a_async_enabled=True,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_tenant_cache_roundtrip(fake_cache: FakeRedis) -> None:
    tenant = _tenant()
    await tenant_cache.set_cached_tenant(tenant)
    loaded = await tenant_cache.get_cached_tenant(tenant.id)
    assert loaded is not None
    assert loaded.slug == "acme"
    assert loaded.timezone == "Asia/Shanghai"
    assert loaded.a2a_async_enabled is True


@pytest.mark.asyncio
async def test_tenant_cache_bump_invalidates(fake_cache: FakeRedis) -> None:
    tenant = _tenant()
    await tenant_cache.set_cached_tenant(tenant)
    await tenant_cache.bump_tenant_cache(tenant.id)
    assert await tenant_cache.get_cached_tenant(tenant.id) is None


@pytest.mark.asyncio
async def test_im_token_cache_roundtrip(fake_cache: FakeRedis) -> None:
    await im_token_cache.set_cached_im_token("feishu", "cli_abc", "tok-1", ttl=600, secret="sec")
    assert await im_token_cache.get_cached_im_token("feishu", "cli_abc", secret="sec") == "tok-1"
    stored = " ".join(fake_cache.store.keys())
    assert "cli_abc" not in stored
    assert "sec" not in stored
    await im_token_cache.drop_cached_im_token("feishu", "cli_abc", secret="sec")
    assert await im_token_cache.get_cached_im_token("feishu", "cli_abc", secret="sec") is None


@pytest.mark.asyncio
async def test_im_token_cache_isolates_secrets(fake_cache: FakeRedis) -> None:
    await im_token_cache.set_cached_im_token("wecom", "corp1", "tok-a", ttl=600, secret="secret-a")
    await im_token_cache.set_cached_im_token("wecom", "corp1", "tok-b", ttl=600, secret="secret-b")
    assert await im_token_cache.get_cached_im_token("wecom", "corp1", secret="secret-a") == "tok-a"
    assert await im_token_cache.get_cached_im_token("wecom", "corp1", secret="secret-b") == "tok-b"


@pytest.mark.asyncio
async def test_tenant_cache_set_skips_after_bump(fake_cache: FakeRedis) -> None:
    tenant = _tenant()
    observed = await tenant_cache.peek_tenant_version(tenant.id)
    await tenant_cache.bump_tenant_cache(tenant.id)
    await tenant_cache.set_cached_tenant(tenant, observed_ver=observed)
    assert await tenant_cache.get_cached_tenant(tenant.id) is None


@pytest.mark.asyncio
async def test_channel_dedup_shared_uses_redis(fake_cache: FakeRedis) -> None:
    ns, key = "feishu", "evt-1"
    assert await channel_dedup.already_processed_shared(ns, key) is False
    await channel_dedup.mark_processed_shared(ns, key)
    channel_dedup._stores.clear()
    assert await channel_dedup.already_processed_shared(ns, key) is True


@pytest.mark.asyncio
async def test_channel_dedup_redis_down_falls_to_local(fake_cache: FakeRedis) -> None:
    fake_cache.fail = True
    ns, key = "slack", "evt-down"
    assert await channel_dedup.already_processed_shared(ns, key) is False
    await channel_dedup.mark_processed_shared(ns, key)
    assert channel_dedup.already_processed(ns, key) is True
