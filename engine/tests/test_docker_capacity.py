"""Guest Docker calls run on a per-loop thread lane of their own, apart from anyio's default limiter."""

import asyncio
import threading
from contextvars import ContextVar
from functools import partial

import anyio
import anyio.lowlevel
import pytest

from app.config import get_settings
from app.services.docker_capacity import (
    DockerBusyError,
    DockerCapacity,
    DockerPriority,
    background_docker,
    docker_capacity,
    docker_priority,
    run_docker,
    shutdown_docker_capacity,
)

caller: ContextVar[str | None] = ContextVar("lane_test_caller", default=None)


class Gate:
    """Lane calls that block on one threading.Event and wake the loop once ``expected`` of them are blocked."""

    def __init__(self, expected: int = 1) -> None:
        self.expected = expected
        self.entered = anyio.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.outcomes: list[bool] = []
        self._arrived = 0
        self._lock = threading.Lock()
        self._token = anyio.lowlevel.current_token()

    def block(self) -> bool:
        with self._lock:
            self._arrived += 1
            last = self._arrived == self.expected
        if last:
            anyio.from_thread.run_sync(self.entered.set, token=self._token)
        released = self.release.wait(10)
        with self._lock:
            self.outcomes.append(released)
            if len(self.outcomes) == self.expected:
                self.finished.set()
        return released


async def run_recording(capacity: DockerCapacity, order: list[str], name: str, priority: DockerPriority) -> None:
    await capacity.run(order.append, name, priority=priority)


def one_slot() -> DockerCapacity:
    capacity = DockerCapacity()
    capacity.limit = 1
    return capacity


async def test_plain_to_thread_work_proceeds_while_every_lane_slot_is_blocked():
    # Given every slot of this loop's Docker lane is held by a call blocked on a threading.Event.
    threads = get_settings().DOCKER_LANE_THREADS
    gate = Gate(threads)
    queued = threading.Event()
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                for _ in range(threads):
                    group.start_soon(run_docker, gate.block)
                await gate.entered.wait()
                group.start_soon(run_docker, queued.set)
                # When ordinary code borrows one of anyio's default worker threads.
                result = await anyio.to_thread.run_sync(lambda: "default thread")
                # Then it completes while the lane is saturated: a further Docker call still waits for a slot.
                assert result == "default thread"
                assert not queued.is_set()
                gate.release.set()
    finally:
        gate.release.set()
        shutdown_docker_capacity()
    assert queued.is_set()
    assert gate.outcomes == [True] * threads


async def test_abandoned_call_returns_while_blocked_and_keeps_its_slot_until_its_thread_returns():
    # Given a one-slot lane whose only call is blocked on a threading.Event.
    capacity = one_slot()
    gate = Gate()
    later = threading.Event()
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(partial(capacity.run, gate.block, abandon_on_cancel=True))
                await gate.entered.wait()
                # When its caller, which may abandon it, is cancelled.
                group.cancel_scope.cancel()
            # Then the caller is back while the thread is still blocked, and that thread still holds the slot.
            assert not gate.finished.is_set()
            async with anyio.create_task_group() as group:
                group.start_soon(capacity.run, later.set)
                await anyio.lowlevel.checkpoint()
                assert not later.is_set()
                gate.release.set()
    finally:
        gate.release.set()
        capacity.shutdown()
    assert later.is_set()
    assert gate.outcomes == [True]


async def test_cancelled_call_still_waiting_for_a_slot_returns_at_once_and_never_runs():
    # Given a one-slot lane whose slot is held by a call blocked on a threading.Event.
    capacity = one_slot()
    gate = Gate()
    skipped, after = threading.Event(), threading.Event()
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(capacity.run, gate.block)
                await gate.entered.wait()
                # When a second call queues behind it and its caller is cancelled.
                async with anyio.create_task_group() as waiting:
                    waiting.start_soon(capacity.run, skipped.set)
                    await anyio.lowlevel.checkpoint()
                    waiting.cancel_scope.cancel()
                # Then that caller is back while the slot is still held.
                assert not gate.finished.is_set()
                gate.release.set()
            await capacity.run(after.set)
    finally:
        gate.release.set()
        capacity.shutdown()
    # And the cancelled call never ran, although a call queued after it did.
    assert after.is_set()
    assert not skipped.is_set()


async def test_cancelled_running_call_waits_for_its_thread_by_default():
    # Given a call blocked on a threading.Event in its lane thread, whose caller may not abandon it.
    capacity = DockerCapacity()
    gate = Gate()
    scope = anyio.CancelScope()
    resumed: list[bool] = []

    async def call() -> None:
        with scope:
            _ = await capacity.run(gate.block)
        resumed.append(gate.finished.is_set())

    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(call)
                await gate.entered.wait()
                # When the caller is cancelled while the thread is still blocked.
                scope.cancel()
                await anyio.lowlevel.checkpoint()
                assert resumed == []
                gate.release.set()
    finally:
        gate.release.set()
        capacity.shutdown()
    # Then it resumed only after the thread had returned, as the caller of a mutation must.
    assert resumed == [True]


def test_each_event_loop_owns_one_lane():
    # Given two event loops, one after the other.
    async def lanes() -> tuple[DockerCapacity, DockerCapacity]:
        return docker_capacity(), docker_capacity()

    # When each asks for its Docker lane twice.
    first, again = asyncio.run(lanes())
    second, _ = asyncio.run(lanes())
    # Then a loop always gets the same lane and another loop gets its own.
    assert first is again
    assert first is not second


async def test_lane_call_runs_off_the_loop_thread_in_the_callers_context():
    # Given a context variable the caller has set.
    _ = caller.set("guest start")
    loop_thread = threading.get_ident()
    # When a Docker call runs on the lane.
    try:
        seen = await run_docker(lambda: (caller.get(), threading.get_ident() != loop_thread))
    finally:
        shutdown_docker_capacity()
    # Then it sees the caller's context, as to_thread would show it, on a thread of its own.
    assert seen == ("guest start", True)


async def test_shutdown_returns_while_a_lane_call_is_blocked():
    # Given a lane call blocked on a threading.Event.
    gate = Gate()
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(partial(run_docker, gate.block, abandon_on_cancel=True))
                await gate.entered.wait()
                # When engine shutdown closes this loop's lane.
                shutdown_docker_capacity()
                # Then it returned without joining the blocked thread.
                assert not gate.finished.is_set()
                group.cancel_scope.cancel()
    finally:
        gate.release.set()


async def test_lane_size_and_queue_bound_come_from_settings(monkeypatch: pytest.MonkeyPatch):
    # Given the configured lane threads and queue bound.
    settings = get_settings()
    monkeypatch.setattr(settings, "DOCKER_LANE_THREADS", 3)
    monkeypatch.setattr(settings, "DOCKER_LANE_QUEUE_LIMIT", 7)
    # When a loop's lane is built.
    capacity = docker_capacity()
    # Then it takes both numbers from them.
    assert (capacity.limit, capacity.queue_limit) == (3, 7)
    shutdown_docker_capacity()


async def test_interactive_calls_are_admitted_ahead_of_queued_background_calls():
    # Given a one-slot lane held by a blocked call, with a background call queued first and an interactive one second.
    capacity = one_slot()
    gate = Gate()
    order: list[str] = []
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(capacity.run, gate.block)
                await gate.entered.wait()
                group.start_soon(run_recording, capacity, order, "background", DockerPriority.BACKGROUND)
                await anyio.lowlevel.checkpoint()
                group.start_soon(run_recording, capacity, order, "interactive", DockerPriority.INTERACTIVE)
                await anyio.lowlevel.checkpoint()
                # When the slot is freed.
                gate.release.set()
    finally:
        gate.release.set()
        capacity.shutdown()
    # Then the interactive call ran first although it queued later.
    assert order == ["interactive", "background"]


async def test_background_docker_context_sets_the_priority_of_nested_calls():
    # Given a one-slot lane held by a blocked call.
    capacity = one_slot()
    gate = Gate()
    order: list[str] = []
    seen: list[DockerPriority] = []

    def record(name: str) -> None:
        order.append(name)
        seen.append(docker_priority.get())

    async def call(name: str) -> None:
        await capacity.run(record, name)

    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(capacity.run, gate.block)
                await gate.entered.wait()
                # When a call made inside background_docker() queues before an ordinary one.
                with background_docker():
                    group.start_soon(call, "background")
                await anyio.lowlevel.checkpoint()
                group.start_soon(call, "interactive")
                await anyio.lowlevel.checkpoint()
                gate.release.set()
    finally:
        gate.release.set()
        capacity.shutdown()
    # Then it queued behind it, and its thread saw the background priority.
    assert order == ["interactive", "background"]
    assert seen == [DockerPriority.INTERACTIVE, DockerPriority.BACKGROUND]
    assert docker_priority.get() is DockerPriority.INTERACTIVE


async def test_full_queue_refuses_the_next_call_of_its_priority_only():
    # Given a one-slot lane that may queue one call per priority, with its slot and its background place taken.
    capacity = DockerCapacity(limit=1, queue_limit=1)
    gate = Gate()
    refused, queued = threading.Event(), threading.Event()
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(capacity.run, gate.block)
                await gate.entered.wait()
                group.start_soon(partial(capacity.run, queued.set, priority=DockerPriority.BACKGROUND))
                await anyio.lowlevel.checkpoint()
                # When another background call arrives.
                with pytest.raises(DockerBusyError):
                    await capacity.run(refused.set, priority=DockerPriority.BACKGROUND)
                # Then it is refused at once, while an interactive call still queues.
                group.start_soon(capacity.run, refused.set)
                await anyio.lowlevel.checkpoint()
                assert not refused.is_set()
                gate.release.set()
    finally:
        gate.release.set()
        capacity.shutdown()
    assert queued.is_set()
    assert refused.is_set()


async def test_cancelled_queued_call_gives_its_queue_place_back():
    # Given a one-slot lane with one queue place, held by a blocked call and one waiting call.
    capacity = DockerCapacity(limit=1, queue_limit=1)
    gate = Gate()
    later = threading.Event()
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(capacity.run, gate.block)
                await gate.entered.wait()
                async with anyio.create_task_group() as waiting:
                    waiting.start_soon(capacity.run, threading.Event().set)
                    await anyio.lowlevel.checkpoint()
                    # When the waiting call is cancelled.
                    waiting.cancel_scope.cancel()
                # Then the next call is queued instead of refused.
                group.start_soon(capacity.run, later.set)
                await anyio.lowlevel.checkpoint()
                gate.release.set()
    finally:
        gate.release.set()
        capacity.shutdown()
    assert later.is_set()
