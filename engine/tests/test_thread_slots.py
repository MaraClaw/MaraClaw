import asyncio

import pytest

from app.core.thread_slots import SlotsBusyError, ThreadSlots


def test_sync_acquire_reports_a_full_gate_without_blocking():
    slots = ThreadSlots(1)
    assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    assert not slots.acquire(timeout=0)
    slots.release()
    assert slots.acquire(timeout=0)


def test_release_without_a_holder_is_an_error():
    with pytest.raises(ValueError, match="released too many times"):
        ThreadSlots(2).release()


def test_a_gate_needs_at_least_one_slot():
    with pytest.raises(ValueError, match="at least one slot"):
        ThreadSlots(0)


async def test_a_task_waits_for_a_release_made_by_another_thread():
    slots = ThreadSlots(1)
    assert slots.acquire(blocking=False)
    waiter = asyncio.create_task(slots.acquire_async())
    await asyncio.sleep(0)
    assert not waiter.done()
    await asyncio.to_thread(slots.release)
    await asyncio.wait_for(waiter, 10)
    assert not slots.acquire(blocking=False)


async def test_tasks_receive_slots_in_arrival_order():
    slots = ThreadSlots(1)
    assert slots.acquire(blocking=False)
    order: list[int] = []

    async def take(number: int) -> None:
        await slots.acquire_async()
        order.append(number)
        slots.release()

    tasks = [asyncio.create_task(take(number)) for number in (1, 2, 3)]
    await asyncio.sleep(0)
    slots.release()
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    assert order == [1, 2, 3]


async def test_a_task_cancelled_while_waiting_holds_nothing():
    slots = ThreadSlots(1)
    assert slots.acquire(blocking=False)
    waiter = asyncio.create_task(slots.acquire_async())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    slots.release()
    assert slots.acquire(blocking=False)


async def test_a_slot_granted_to_a_task_cancelled_before_it_resumes_goes_back():
    slots = ThreadSlots(1)
    assert slots.acquire(blocking=False)
    waiter = asyncio.create_task(slots.acquire_async())
    await asyncio.sleep(0)
    slots.release()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert slots.acquire(blocking=False)


async def _queue(slots: ThreadSlots, count: int) -> list[asyncio.Task[None]]:
    queued: list[asyncio.Task[None]] = [asyncio.create_task(slots.acquire_async()) for _ in range(count)]
    await asyncio.sleep(0)
    return queued


async def test_a_full_wait_queue_refuses_the_next_task_and_the_queued_ones_are_still_served_in_order():
    slots = ThreadSlots(1, max_waiters=2)
    assert slots.acquire(blocking=False)
    first, second = await _queue(slots, 2)
    with pytest.raises(SlotsBusyError):
        await slots.acquire_async()
    slots.release()
    await asyncio.wait_for(first, 10)
    assert not second.done()
    slots.release()
    await asyncio.wait_for(second, 10)
    slots.release()
    assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)


async def test_a_cancelled_waiter_frees_its_place_in_a_full_queue():
    slots = ThreadSlots(1, max_waiters=1)
    assert slots.acquire(blocking=False)
    (waiter,) = await _queue(slots, 1)
    with pytest.raises(SlotsBusyError):
        await slots.acquire_async()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    (replacement,) = await _queue(slots, 1)
    assert not replacement.done()
    slots.release()
    await asyncio.wait_for(replacement, 10)


async def test_without_a_limit_every_waiter_queues():
    slots = ThreadSlots(1)
    assert slots.max_waiters is None
    assert slots.acquire(blocking=False)
    waiters = await _queue(slots, 100)
    assert not any(waiter.done() for waiter in waiters)
    for _ in range(101):
        slots.release()
    await asyncio.wait_for(asyncio.gather(*waiters), 10)
    assert slots.acquire(blocking=False)


async def test_a_zero_limit_still_admits_a_free_slot_but_never_queues():
    slots = ThreadSlots(1, max_waiters=0)
    await slots.acquire_async()
    with pytest.raises(SlotsBusyError):
        await slots.acquire_async()
    slots.release()
    await slots.acquire_async()


def test_the_limit_bounds_only_waiting_tasks_not_thread_acquires():
    slots = ThreadSlots(1, max_waiters=0)
    assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    assert not slots.acquire(timeout=0)


def test_a_negative_waiter_limit_is_an_error():
    with pytest.raises(ValueError, match="cannot be negative"):
        ThreadSlots(1, max_waiters=-1)
