import asyncio

import pytest

from app.core.thread_slots import ThreadSlots


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
