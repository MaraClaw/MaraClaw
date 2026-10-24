"""OpenClaw worker scheduling with controlled clock and blocked artifact reconciliation."""

from pathlib import Path
from uuid import UUID, uuid4

import anyio
import pytest

from app.records.openclaw_update import OpenClawUpdateRunRecord
from app.services import openclaw_update_worker as worker
from app.services.openclaw_update_executor import UpgradeExecutor
from openclaw_update_fakes import FakeDocker, FakeManager, FakeStore


async def test_startup_sweep_does_not_block_active_run_polling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given reconciliation remains blocked while active runs are available.
    run = OpenClawUpdateRunRecord(id=uuid4(), notice_id=uuid4(), agent_id=uuid4(), state="pending")
    store = FakeStore({}, {run.id: run})
    docker = FakeDocker()
    executor = UpgradeExecutor(store, docker, FakeManager(docker, tmp_path))
    sweeping, progressed = anyio.Event(), anyio.Event()

    async def blocked_sweep() -> None:
        sweeping.set()
        await anyio.sleep_forever()

    async def process(_run_id: UUID) -> None:
        if sweeping.is_set():
            progressed.set()

    async def clock_tick(_seconds: float) -> None:
        await anyio.lowlevel.checkpoint()

    def make_executor(_store: object, _docker: object, _manager: object) -> UpgradeExecutor:
        return executor

    monkeypatch.setattr(worker, "agent_manager", FakeManager(docker, tmp_path))
    monkeypatch.setattr(worker, "PostgresUpgradeStore", lambda: store)
    monkeypatch.setattr(worker, "UpgradeExecutor", make_executor)
    monkeypatch.setattr(executor, "process", process)
    monkeypatch.setattr(executor, "reconcile_success_artifacts", blocked_sweep)
    monkeypatch.setattr(worker.anyio, "sleep", clock_tick)
    # When the real worker starts both loops and its poll clock advances.
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(worker.start_openclaw_update_worker)
            await progressed.wait()
            group.cancel_scope.cancel()
    # Then an active run was processed while the startup sweep was still blocked.
    assert sweeping.is_set()
    assert progressed.is_set()


async def test_sweep_retries_failure_only_on_its_slower_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the startup sweep fails once and a controlled clock advances its retry interval.
    docker = FakeDocker()
    executor = UpgradeExecutor(FakeStore({}, {}), docker, FakeManager(docker, tmp_path))
    retried = anyio.Event()
    events: list[str | float] = []

    async def reconcile() -> None:
        events.append("sweep")
        if len(events) == 1:
            raise OSError("temporary sweep failure")
        retried.set()

    async def advance(seconds: float) -> None:
        events.append(seconds)
        if retried.is_set():
            await anyio.sleep_forever()
        await anyio.lowlevel.checkpoint()

    monkeypatch.setattr(executor, "reconcile_success_artifacts", reconcile)
    monkeypatch.setattr(worker.anyio, "sleep", advance)
    # When the independent sweep loop starts, fails, waits, and retries.
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(worker._sweep_success_artifacts, executor)
            await retried.wait()
            group.cancel_scope.cancel()
    # Then startup ran before any wait, and retry used a cadence longer than active polling.
    assert events == ["sweep", worker.ARTIFACT_SWEEP_SECONDS, "sweep", worker.ARTIFACT_SWEEP_SECONDS]
    assert worker.ARTIFACT_SWEEP_SECONDS > worker.POLL_SECONDS
