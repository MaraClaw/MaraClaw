import asyncio
from collections.abc import Callable
from datetime import datetime
from functools import partial
from uuid import uuid4

import anyio
import pytest

from app.records.gateway_report_delivery import GatewayReportDeliveryRecord
from app.services import gateway_report_relay as relay, gateway_report_wake as wake
from gateway_report_relay_fakes import RelayStore

# Only a wake can end the idle wait inside these tests.
FALLBACK = 30.0
BOUND = 0.5


class FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.publish_attempted = anyio.Event()
        self.fail = False

    async def publish(self, channel: str, data: str) -> int:
        self.publish_attempted.set()
        if self.fail:
            raise ConnectionError("redis down")
        self.published.append((channel, data))
        return 1


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    value = FakeRedis()

    async def get_redis() -> FakeRedis:
        return value

    monkeypatch.setattr(wake, "get_redis", get_redis)
    return value


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RelayStore:
    value = RelayStore()
    value.install(monkeypatch)
    return value


def _spy_claims(store: RelayStore, monkeypatch: pytest.MonkeyPatch) -> tuple[anyio.Event, anyio.Event, list[int]]:
    """Events set after a poll finds nothing and after a poll finds work; the list records each poll's limit."""
    idle_poll, claimed, limits = anyio.Event(), anyio.Event(), list[int]()
    real = store.claim_batch

    async def claim_batch(*, limit: int, lease_seconds: float, now: datetime) -> list[GatewayReportDeliveryRecord]:
        limits.append(limit)
        rows = await real(limit=limit, lease_seconds=lease_seconds, now=now)
        (claimed if rows else idle_poll).set()
        return rows

    monkeypatch.setattr(store, "claim_batch", claim_batch)
    return idle_poll, claimed, limits


async def test_intent_created_after_idle_is_claimed_by_the_signal_alone(
    store: RelayStore, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the loop has polled an empty queue and gone idle with a 30 s fallback.
    idle_poll, claimed, _ = _spy_claims(store, monkeypatch)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(partial(relay.start_gateway_report_relay, idle_timeout=FALLBACK, clock=store.clock))
        with anyio.fail_after(BOUND):
            await idle_poll.wait()
            await anyio.wait_all_tasks_blocked()
        row = store.intent()
        # When a committed intent is announced.
        wake.wake_gateway_report_relay()
        # Then it is claimed and delivered long before the fallback, and the wake crossed processes via Redis.
        with anyio.fail_after(BOUND):
            await claimed.wait()
            await redis.publish_attempted.wait()
        assert store.rows[row.id].attempts == 1
        tasks.cancel_scope.cancel()
    assert redis.published[0][0] == "gateway_report_relay:wake"


async def test_publish_failure_never_raises_and_still_wakes_the_local_relay(redis: FakeRedis) -> None:
    # Given Redis is down.
    redis.fail = True
    with wake.listen() as event:
        # When the wake is scheduled through the background-task path used by settlement.
        wake.wake_gateway_report_relay()
        # Then the local signal still fires and the failed publish is contained.
        with anyio.fail_after(BOUND):
            _ = await event.wait()
            await redis.publish_attempted.wait()
        await wake._announce()


async def test_slow_delivery_does_not_block_claiming_a_new_intent(
    store: RelayStore, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one delivery that never finishes.
    gate, entered, finished = anyio.Event(), anyio.Event(), anyio.Event()
    slow = store.intent()

    async def deliver_claim(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime]) -> None:
        if claim.id == slow.id:
            entered.set()
            await gate.wait()
        else:
            finished.set()

    monkeypatch.setattr(relay, "deliver_claim", deliver_claim)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(partial(relay.start_gateway_report_relay, idle_timeout=FALLBACK, clock=store.clock))
        with anyio.fail_after(BOUND):
            await entered.wait()
            await anyio.wait_all_tasks_blocked()
        # When a second intent arrives while the first is still running.
        store.intent()
        wake.wake_gateway_report_relay()
        # Then it is claimed and finished without waiting for the first.
        with anyio.fail_after(BOUND):
            await finished.wait()
        assert not gate.is_set()
        tasks.cancel_scope.cancel()


async def test_in_flight_deliveries_are_bounded_and_freed_slots_are_refilled(
    store: RelayStore, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given more due intents than slots and deliveries that hold their slot.
    for _ in range(relay.MAX_IN_FLIGHT + 5):
        _ = store.intent()
    started = list[GatewayReportDeliveryRecord]()
    releases = dict[object, anyio.Event]()

    async def deliver_claim(claim: GatewayReportDeliveryRecord, clock: Callable[[], datetime]) -> None:
        releases[claim.id] = anyio.Event()
        started.append(claim)
        await releases[claim.id].wait()

    monkeypatch.setattr(relay, "deliver_claim", deliver_claim)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(partial(relay.start_gateway_report_relay, idle_timeout=FALLBACK, clock=store.clock))
        # When the loop has done all it can.
        with anyio.fail_after(BOUND):
            while len(started) < relay.MAX_IN_FLIGHT:
                await anyio.wait_all_tasks_blocked()
            await anyio.wait_all_tasks_blocked()
        # Then exactly the cap is running, and finishing one claims exactly one more.
        assert len(started) == relay.MAX_IN_FLIGHT
        releases[started[0].id].set()
        with anyio.fail_after(BOUND):
            while len(started) < relay.MAX_IN_FLIGHT + 1:
                await anyio.wait_all_tasks_blocked()
        await anyio.wait_all_tasks_blocked()
        assert len(started) == relay.MAX_IN_FLIGHT + 1
        tasks.cancel_scope.cancel()


class FakePubSub:
    def __init__(self, outcomes: asyncio.Queue[object], *, fail: bool) -> None:
        self.outcomes = outcomes
        self.fail = fail
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        assert channel == "gateway_report_relay:wake"
        if self.fail:
            raise ConnectionError("lost")

    async def get_message(self, **_options: object) -> object:
        outcome = await self.outcomes.get()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


async def test_subscriber_wakes_on_foreign_messages_and_reconnects_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a first connection that fails, then a healthy one.
    monkeypatch.setattr(wake, "_RESUBSCRIBE_MIN", 0.0)
    outcomes: asyncio.Queue[object] = asyncio.Queue()
    pubsubs = list[FakePubSub]()

    class Client:
        def pubsub(self) -> FakePubSub:
            pubsubs.append(FakePubSub(outcomes, fail=not pubsubs))
            return pubsubs[-1]

    async def get_redis() -> Client:
        return Client()

    monkeypatch.setattr(wake, "get_redis", get_redis)
    with wake.listen() as event:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wake.subscribe_wakes)
            # When the resubscription succeeds it polls once, then a message from this process is ignored.
            with anyio.fail_after(BOUND):
                _ = await event.wait()
            event.clear()
            outcomes.put_nowait({"type": "message", "data": wake._ORIGIN.encode()})
            await anyio.wait_all_tasks_blocked()
            assert not event.is_set()
            # Then a message from another process wakes the loop.
            outcomes.put_nowait({"type": "message", "data": uuid4().hex.encode()})
            with anyio.fail_after(BOUND):
                _ = await event.wait()
            tasks.cancel_scope.cancel()
    assert len(pubsubs) == 2
    assert all(p.closed for p in pubsubs)
