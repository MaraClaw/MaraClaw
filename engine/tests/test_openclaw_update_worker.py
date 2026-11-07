"""OpenClaw worker scheduling with controlled clock and blocked artifact reconciliation."""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import pytest

from app.records.openclaw_update import OpenClawUpdateRunRecord
from app.runtime.tasks import OwnedTasks
from app.services import openclaw_update_worker as worker
from app.services.openclaw_update_executor import UpgradeExecutor
from openclaw_update_fakes import FakeDocker, FakeManager, FakeStore
from progress_fakes import FakeClock, Pacer


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


class DockerlessManager:
    """Shared ``agent_manager`` stand-in whose Docker appears only after ``available`` is set."""

    def __init__(self, docker: FakeDocker) -> None:
        self.docker_client: FakeDocker | None = None
        self.available: bool = False
        self._docker: FakeDocker = docker
        self.reacquisitions: int = 0

    def reacquire_docker_client(self) -> FakeDocker | None:
        self.reacquisitions += 1
        if self.available and self.docker_client is None:
            self.docker_client = self._docker
        return self.docker_client


class WorkerHarness:
    """Runs the real worker as a budgeted owned loop on a fake clock and a parked sleep."""

    budget = 100.0

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = FakeClock()
        self.tasks = OwnedTasks(self.clock)
        self.pacer = Pacer()
        self.docker = FakeDocker()
        self.manager = DockerlessManager(self.docker)
        self.store = FakeStore({}, {})
        self.executor = UpgradeExecutor(self.store, self.docker, FakeManager(self.docker, root))
        self.built_with: list[FakeDocker | None] = []

        def make_executor(_store: object, _docker: object, _manager: object) -> UpgradeExecutor:
            self.built_with.append(self.manager.docker_client)
            return self.executor

        async def blocked_sweep() -> None:
            await anyio.sleep_forever()

        monkeypatch.setattr(worker, "agent_manager", self.manager)
        monkeypatch.setattr(worker, "PostgresUpgradeStore", lambda: self.store)
        monkeypatch.setattr(worker, "UpgradeExecutor", make_executor)
        monkeypatch.setattr(self.executor, "reconcile_success_artifacts", blocked_sweep)
        monkeypatch.setattr(worker.anyio, "sleep", self.pacer.sleep)

    def add_run(self) -> OpenClawUpdateRunRecord:
        run = OpenClawUpdateRunRecord(id=uuid4(), notice_id=uuid4(), agent_id=uuid4(), state="pending")
        self.store.runs[run.id] = run
        return run

    async def start(self) -> None:
        self.tasks.start(
            "openclaw_update",
            worker.start_openclaw_update_worker(),
            requirement="loop",
            progress_budget_seconds=self.budget,
        )
        await self.pacer.parked()

    async def advance(self, seconds: float) -> None:
        self.clock.now += seconds
        await self.pacer.release()


@pytest.fixture
def stalled_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkerHarness:
    return WorkerHarness(tmp_path, monkeypatch)


async def test_no_docker_without_active_runs_stays_ready_and_backs_off_with_a_cap(
    stalled_worker: WorkerHarness,
) -> None:
    # Given a dev box without Docker and nothing to upgrade.
    harness = stalled_worker
    await harness.start()
    # When far more than the budget passes across many retries.
    for _ in range(8):
        await harness.advance(harness.budget)
        assert harness.tasks.ready()
    # Then the worker kept retrying Docker with capped backoff and never failed readiness.
    assert harness.manager.reacquisitions == 9
    assert harness.pacer.waits == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0, 60.0]
    assert harness.built_with == []
    await harness.tasks.stop()


async def test_no_docker_with_active_runs_goes_stale_then_recovers_once_docker_is_installed_on_the_manager(
    stalled_worker: WorkerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an active run and no Docker.
    harness = stalled_worker
    run = harness.add_run()
    processed: list[UUID] = []

    async def process(run_id: UUID) -> None:
        processed.append(run_id)

    monkeypatch.setattr(harness.executor, "process", process)
    await harness.start()
    # When the budget lapses without Docker.
    await harness.advance(harness.budget - 1)
    assert harness.tasks.ready()
    await harness.advance(2)
    # Then the stalled worker fails readiness without being cancelled.
    assert not harness.tasks.ready()
    assert not harness.tasks.required["openclaw_update"][0].done()
    # When Docker becomes reachable.
    harness.manager.available = True
    await harness.pacer.release()
    # Then the shared manager holds the client before the executor is built, the run is processed and readiness recovers.
    assert harness.manager.docker_client is harness.docker
    assert harness.built_with == [harness.docker]
    assert processed == [run.id]
    assert harness.tasks.ready()
    await harness.tasks.stop()


async def test_time_inside_executor_process_is_exempt_and_never_cancelled(
    stalled_worker: WorkerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given Docker is available and an upgrade cutover that outlasts many budgets.
    harness = stalled_worker
    harness.manager.docker_client = harness.docker
    run = harness.add_run()
    inside, finish, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished: list[UUID] = []

    async def process(run_id: UUID) -> None:
        inside.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finished.append(run_id)

    monkeypatch.setattr(harness.executor, "process", process)
    harness.tasks.start(
        "openclaw_update",
        worker.start_openclaw_update_worker(),
        requirement="loop",
        progress_budget_seconds=harness.budget,
    )
    await asyncio.wait_for(inside.wait(), 5)
    # When ten budgets elapse inside the cutover.
    harness.clock.now += harness.budget * 10
    # Then readiness holds and nothing cancelled or timed out the run.
    assert harness.tasks.ready()
    assert not cancelled.is_set()
    # When it completes, the budget restarts from then and the next pass is parked on the poll sleep.
    finish.set()
    await harness.pacer.parked()
    assert finished == [run.id]
    assert harness.tasks.ready()
    harness.clock.now += harness.budget - 1
    assert harness.tasks.ready()
    harness.clock.now += 2
    assert not harness.tasks.ready()
    await harness.tasks.stop()
