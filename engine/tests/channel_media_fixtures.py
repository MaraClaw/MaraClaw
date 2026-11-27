from __future__ import annotations

import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import override

import anyio
import httpx
import pytest

from app.config import get_settings
from app.core import redis_cache
from app.services.channels import submissions
from app.services.channels.media_limits import READ_CHUNK_BYTES, MediaBudget

# Fails a test whose stream was never cut off or released, instead of leaving it hanging.
HANG_GUARD_SECONDS = 5

# Captured at import so a test that patches ``httpx.AsyncClient`` can still build the real one.
REAL_CLIENT = httpx.AsyncClient


def set_attachment_limit(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    """Lower the per-message attachment count so a byte-limit check alone cannot satisfy a test."""
    monkeypatch.setattr(get_settings(), "CHANNEL_MEDIA_MAX_ATTACHMENTS", limit)


class CountingStream(httpx.AsyncByteStream):
    """A body of ``total`` bytes without Content-Length that records how much the reader pulled."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.consumed = 0

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        block = b"x" * READ_CHUNK_BYTES
        while self.consumed < self.total:
            self.consumed += len(block)
            yield block


class FakeClock:
    """A monotonic clock a test moves by hand, so a deadline never costs real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def ack_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Run every acknowledgement budget built during the test on a clock the test moves."""
    clock = FakeClock()
    original = MediaBudget.for_acknowledgement
    monkeypatch.setattr(MediaBudget, "for_acknowledgement", lambda: original(clock))
    return clock


def set_ack_budget(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setattr(get_settings(), "CHANNEL_MEDIA_ACK_BUDGET_SECONDS", seconds)


class SlowStream(httpx.AsyncByteStream):
    """A body of ``total`` bytes that takes ``step`` clock seconds per read chunk and records how much the reader pulled."""

    def __init__(self, clock: FakeClock, total: int, step: float) -> None:
        self.clock = clock
        self.total = total
        self.step = step
        self.consumed = 0

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        block = b"x" * READ_CHUNK_BYTES
        while self.consumed < self.total:
            self.clock.advance(self.step)
            self.consumed += len(block)
            yield block


class StalledStream(httpx.AsyncByteStream):
    """A body whose host accepted the request and then never sent a byte; the reader's own deadline must cut it off."""

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        with anyio.fail_after(HANG_GUARD_SECONDS):
            await anyio.Event().wait()
        yield b""


class ClockedStream(httpx.AsyncByteStream):
    """A short body that moves ``clock`` by ``seconds`` after its last byte, so the deadline can pass between fetch and store."""

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        self.clock = clock
        self.seconds = seconds

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"file bytes"
        self.clock.advance(self.seconds)


type Store = Callable[..., Awaitable[tuple[str, str, object]]]


def store_overrunning_the_deadline(clock: FakeClock, store: Store, seconds: float = 3.0) -> Store:
    """Wrap a ``store_agent_upload`` so ``clock`` passes the deadline while the store is running.

    Fails the test if the store runs under a cancel scope with a deadline: a store that has started must not be cancellable.
    """

    async def run(*args: object, **kwargs: object) -> tuple[str, str, object]:
        assert anyio.current_effective_deadline() == math.inf
        result = await store(*args, **kwargs)
        clock.advance(seconds)
        return result

    return run


def assert_in_flight_refusal(response: httpx.Response) -> None:
    """A delivery refused because another still holds its event's claim: retryable 503, ``Retry-After``, stable code."""
    lifetime = get_settings().CHANNEL_MEDIA_ACK_BUDGET_SECONDS + submissions.CLAIM_GRACE_SECONDS
    assert response.status_code == 503
    assert response.headers["retry-after"] == str(math.ceil(lifetime))
    assert response.json()["detail"]["code"] == submissions.CHANNEL_EVENT_IN_FLIGHT


class GatedStream(httpx.AsyncByteStream):
    """A body that sends nothing until the test releases it, so a delivery can be held in the middle of a download."""

    def __init__(self) -> None:
        self.started = anyio.Event()
        self.release = anyio.Event()

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        with anyio.fail_after(HANG_GUARD_SECONDS):
            await self.release.wait()
        yield b"file bytes"


class ClaimStore:
    """The cache Redis as the in-flight claim uses it (SET NX with a TTL, compare-and-delete), aged by hand."""

    def __init__(self) -> None:
        self.claims: dict[str, tuple[str, float]] = {}
        self.lifetimes: dict[str, float] = {}
        self.taken = 0
        self.now = 0.0
        self.failure: Exception | None = None
        self.lose_releases = False

    async def set(self, key: str, value: str, *, px: int, nx: bool) -> bool | None:
        if self.failure is not None:
            raise self.failure
        assert nx
        if key in self.claims:
            return None
        self.claims[key] = (value, self.now + px / 1000)
        self.lifetimes[key] = px / 1000
        self.taken += 1
        return True

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        if self.failure is not None:
            raise self.failure
        held = self.claims.get(key)
        if self.lose_releases or held is None or held[0] != token:
            return 0
        del self.claims[key]
        return 1

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.claims = {key: held for key, held in self.claims.items() if held[1] > self.now}


@pytest.fixture
def claim_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[ClaimStore]:
    store = ClaimStore()

    async def get_cache_redis() -> ClaimStore:
        return store

    redis_cache.reset_circuit()
    monkeypatch.setattr(submissions, "get_cache_redis", get_cache_redis)
    yield store
    redis_cache.reset_circuit()


def serve(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Route every ``httpx.AsyncClient`` the code under test builds to ``handler``."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: REAL_CLIENT(transport=transport))
