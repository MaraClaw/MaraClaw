import asyncio

import pytest

from app.runtime.tasks import AdmissionClosedError, TaskAdmission


def admission() -> TaskAdmission:
    return TaskAdmission("Test", set())


def track(gate: TaskAdmission, task: asyncio.Task[None]) -> None:
    gate.tasks.add(task)
    task.add_done_callback(gate.tasks.discard)


async def test_closed_gate_refuses_work_until_reopened() -> None:
    # Given a gate closed by a drain
    gate = admission()
    gate.close()
    ran: list[str] = []

    async def work() -> str:
        ran.append("ran")
        return "done"

    # When / Then the work is refused without running, and runs again once reopened
    with pytest.raises(AdmissionClosedError):
        await gate.run(work)
    assert ran == []
    gate.open()
    assert await gate.run(work) == "done"


async def test_work_admitted_before_close_runs_to_completion_and_drain_waits_for_it() -> None:
    # Given admitted work suspended mid-flight
    gate = admission()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def work() -> str:
        entered.set()
        await release.wait()
        return "done"

    caller = asyncio.create_task(gate.run(work))
    await entered.wait()
    # When the gate closes and a drain starts
    gate.close()
    drain = asyncio.create_task(gate.drain(grace_seconds=1))
    await asyncio.sleep(0.01)
    assert not drain.done()
    release.set()
    await drain
    # Then the work was not refused or cancelled
    assert await caller == "done"


async def test_task_started_by_admitted_work_during_drain_is_joined_not_cancelled() -> None:
    # Given admitted work that spawns a tracked task only after the drain began
    gate = admission()
    entered = asyncio.Event()
    release = asyncio.Event()
    spawned = asyncio.Event()
    finished = asyncio.Event()

    async def child() -> None:
        spawned.set()
        await release.wait()
        finished.set()

    async def work() -> None:
        entered.set()
        await release.wait()
        track(gate, asyncio.create_task(child()))

    caller = asyncio.create_task(gate.run(work))
    await entered.wait()
    gate.close()
    drain = asyncio.create_task(gate.drain(grace_seconds=1))
    await asyncio.sleep(0)
    release.set()
    await spawned.wait()
    # When
    await drain
    await caller
    # Then the late task finished inside the grace period
    assert finished.is_set()
    assert not gate.tasks


async def test_work_outliving_grace_is_cancelled_and_awaited_before_drain_returns() -> None:
    # Given admitted work that never finishes and releases a resource in a finalizer
    gate = admission()
    entered = asyncio.Event()
    finalized = asyncio.Event()
    spawned: list[str] = []

    async def work() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()
        spawned.append("never")

    caller = asyncio.create_task(gate.run(work))
    await entered.wait()
    # When the drain runs out of grace
    await gate.drain(grace_seconds=0.01)
    # Then the finalizer already ran, and the caller sees a refusal rather than its own cancellation
    assert finalized.is_set()
    with pytest.raises(AdmissionClosedError):
        await caller
    assert spawned == []


async def test_caller_cancellation_reaches_the_work_and_stays_a_cancellation() -> None:
    # Given work awaited by a caller
    gate = admission()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def work() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    caller = asyncio.create_task(gate.run(work))
    await entered.wait()
    # When the caller itself is cancelled
    caller.cancel()
    # Then the work is cancelled first and the caller still sees CancelledError
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert cancelled.is_set()


async def test_drain_cancels_only_owned_tasks_after_grace() -> None:
    # Given an owned task that outlives the grace period and a task owned by someone else
    gate = admission()
    stopped = asyncio.Event()
    foreign_stop = asyncio.Event()

    async def stuck() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    foreign = asyncio.create_task(foreign_stop.wait())
    track(gate, asyncio.create_task(stuck()))
    await asyncio.sleep(0)
    # When
    await gate.drain(grace_seconds=0.01)
    # Then
    assert stopped.is_set()
    assert not gate.tasks
    assert not foreign.done()
    foreign_stop.set()
    await foreign


async def test_drain_with_nothing_owned_returns() -> None:
    gate = admission()
    await gate.drain(grace_seconds=0)
    assert not gate.accepting


@pytest.mark.parametrize("grace", [0, 0.01])
async def test_drain_logs_but_survives_task_failures(grace: float) -> None:
    # Given a task that fails while being cancelled
    gate = admission()

    async def failing() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("cleanup failed")

    task = asyncio.create_task(failing())
    track(gate, task)
    await asyncio.sleep(0)
    # When / Then
    await gate.drain(grace_seconds=grace)
    assert task.done()
