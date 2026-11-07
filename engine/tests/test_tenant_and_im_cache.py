"""Tenant row cache, IM token cache, and shared channel dedup."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
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
    await tenant_cache.set_cached_tenant(tenant, observed_ver=await tenant_cache.peek_tenant_version(tenant.id))
    loaded = await tenant_cache.get_cached_tenant(tenant.id)
    assert loaded is not None
    assert loaded.slug == "acme"
    assert loaded.timezone == "Asia/Shanghai"
    assert loaded.a2a_async_enabled is True


@pytest.mark.asyncio
async def test_tenant_cache_bump_invalidates(fake_cache: FakeRedis) -> None:
    tenant = _tenant()
    await tenant_cache.set_cached_tenant(tenant, observed_ver=await tenant_cache.peek_tenant_version(tenant.id))
    await tenant_cache.bump_tenant_cache(tenant.id)
    assert await tenant_cache.get_cached_tenant(tenant.id) is None


async def test_tenant_misses_when_only_generation_read_fails(fake_cache: FakeRedis) -> None:
    tenant = _tenant()
    fake_cache.store[tenant_cache._ver_key(tenant.id)] = "0"
    await tenant_cache.set_cached_tenant(tenant, observed_ver=await tenant_cache.peek_tenant_version(tenant.id))
    fake_cache.fail_gets.add(tenant_cache._ver_key(tenant.id))

    loaded = await tenant_cache.get_cached_tenant(tenant.id)

    assert loaded is None


@pytest.mark.parametrize("recreate", [False, True])
async def test_expired_generation_cannot_revalidate_old_tenant(fake_cache: FakeRedis, recreate: bool) -> None:
    tenant = _tenant()
    key = tenant_cache._ver_key(tenant.id)
    await redis_cache.bump_version(key)
    await tenant_cache.set_cached_tenant(tenant, observed_ver=await tenant_cache.peek_tenant_version(tenant.id))
    fake_cache.store.pop(key)

    if recreate:
        await redis_cache.bump_version(key)
    loaded = await tenant_cache.get_cached_tenant(tenant.id)

    assert loaded is None


@pytest.mark.parametrize("after_observation", [False, True])
async def test_tenant_fill_requires_successful_generation_observations(
    fake_cache: FakeRedis, after_observation: bool
) -> None:
    tenant = _tenant()
    observed = await tenant_cache.peek_tenant_version(tenant.id) if after_observation else None
    fake_cache.fail_gets.add(tenant_cache._ver_key(tenant.id))
    if not after_observation:
        observed = await tenant_cache.peek_tenant_version(tenant.id)
        fake_cache.fail_gets.clear()
        redis_cache.reset_circuit()

    await tenant_cache.set_cached_tenant(tenant, observed_ver=observed)

    assert tenant_cache._row_key(tenant.id) not in fake_cache.store


async def test_tenant_fill_refuses_missing_current_generation(fake_cache: FakeRedis) -> None:
    tenant = _tenant()
    observed = await tenant_cache.peek_tenant_version(tenant.id)
    fake_cache.store.pop(tenant_cache._ver_key(tenant.id))

    await tenant_cache.set_cached_tenant(tenant, observed_ver=observed)

    assert tenant_cache._row_key(tenant.id) not in fake_cache.store


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


class Mint:
    """A token mint over a MockTransport that parks every request until `gate` opens."""

    def __init__(self) -> None:
        self.calls: int = 0
        self.status: int = 200
        self.gate: asyncio.Event = asyncio.Event()
        self.started: asyncio.Event = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.started.set()
        await self.gate.wait()
        return httpx.Response(self.status, json={"token": f"tok-{self.calls}"})

    async def __call__(self) -> im_token_cache.MintedToken:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as client:
            response = await client.get("https://idp.test/token")
        if response.is_error:
            raise im_token_cache.ImTokenRefusedError(response)
        return im_token_cache.MintedToken(str(response.json()["token"]), 7200)


KEY = im_token_cache.ImTokenKey("wecom", "corp1", "secret-a")
STORED = ("wecom", "corp1", "secret-a")


async def test_concurrent_misses_share_one_mint_per_key(fake_cache: FakeRedis) -> None:
    mint = Mint()
    callers = [asyncio.create_task(im_token_cache.get_or_mint_im_token(KEY, mint)) for _ in range(5)]
    async with asyncio.timeout(5):
        await mint.started.wait()
    mint.gate.set()

    assert await asyncio.gather(*callers) == ["tok-1"] * 5
    assert mint.calls == 1
    assert await im_token_cache.get_cached_im_token(*STORED[:2], secret=STORED[2]) == "tok-1"
    assert await im_token_cache.get_or_mint_im_token(KEY, mint) == "tok-1"
    assert mint.calls == 1
    other = im_token_cache.ImTokenKey("wecom", "corp1", "secret-b")
    assert await im_token_cache.get_or_mint_im_token(other, mint) == "tok-2"


async def test_cancelled_leader_leaves_the_mint_running_for_waiters(fake_cache: FakeRedis) -> None:
    mint = Mint()
    leader = asyncio.create_task(im_token_cache.get_or_mint_im_token(KEY, mint))
    waiters = [asyncio.create_task(im_token_cache.get_or_mint_im_token(KEY, mint)) for _ in range(2)]
    async with asyncio.timeout(5):
        await mint.started.wait()
    _ = leader.cancel()
    mint.gate.set()

    assert await asyncio.gather(*waiters) == ["tok-1"] * 2
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert mint.calls == 1
    assert await im_token_cache.get_cached_im_token(*STORED[:2], secret=STORED[2]) == "tok-1"


async def test_failed_mint_reaches_waiters_and_is_never_cached(fake_cache: FakeRedis) -> None:
    mint = Mint()
    mint.status = 503
    callers = [asyncio.create_task(im_token_cache.get_or_mint_im_token(KEY, mint)) for _ in range(3)]
    async with asyncio.timeout(5):
        await mint.started.wait()
    mint.gate.set()

    failures = await asyncio.gather(*callers, return_exceptions=True)
    assert all(isinstance(failure, im_token_cache.ImTokenRefusedError) for failure in failures)
    assert failures[0] is failures[1] is failures[2]
    assert mint.calls == 1
    assert fake_cache.store == {}
    mint.status = 200
    assert await im_token_cache.get_or_mint_im_token(KEY, mint) == "tok-2"


async def test_empty_mint_is_returned_but_not_cached(fake_cache: FakeRedis) -> None:
    async def empty() -> im_token_cache.MintedToken:
        return im_token_cache.MintedToken("")

    assert await im_token_cache.get_or_mint_im_token(KEY, empty) == ""
    assert fake_cache.store == {}


async def test_redis_outage_falls_open_to_a_direct_mint(fake_cache: FakeRedis) -> None:
    mint = Mint()
    mint.gate.set()
    fake_cache.fail = True

    assert await im_token_cache.get_or_mint_im_token(KEY, mint) == "tok-1"
    assert mint.calls == 1


async def test_rejection_drop_only_removes_the_rejected_token(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def compare_and_delete(script: str, numkeys: int, key: str, expected: str) -> int:
        if fake_cache.fail:
            raise RuntimeError("redis down")
        return int(fake_cache.store.get(key) == expected and fake_cache.store.pop(key) is not None)

    monkeypatch.setattr(fake_cache, "eval", compare_and_delete, raising=False)
    await im_token_cache.set_cached_im_token(*STORED[:2], "fresh", ttl=600, secret=STORED[2])

    await im_token_cache.drop_rejected_im_token(KEY, "stale")
    assert await im_token_cache.get_cached_im_token(*STORED[:2], secret=STORED[2]) == "fresh"
    fake_cache.fail = True
    await im_token_cache.drop_rejected_im_token(KEY, "fresh")
    fake_cache.fail = False
    assert await im_token_cache.get_cached_im_token(*STORED[:2], secret=STORED[2]) == "fresh"
    await im_token_cache.drop_rejected_im_token(KEY, "fresh")
    assert await im_token_cache.get_cached_im_token(*STORED[:2], secret=STORED[2]) is None


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
