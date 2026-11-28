import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError

from app.config import get_settings
from app.core.thread_slots import SlotsBusyError
from app.db.errors import DbError
from app.records.agent import AgentRecord
from app.services import workspace_mirror_worker as worker
from app.services.workspace_mirror import PASS_BUDGET_SECONDS, MirrorReconcileError, ReconcileReport
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]


def _agents(count: int) -> list[AgentRecord]:
    return sorted(
        (
            AgentRecord(id=uuid4(), name=f"Agent {number}", creator_id=uuid4(), tenant_id=uuid4())
            for number in range(count)
        ),
        key=lambda agent: agent.id,
    )


class Directory:
    """Scripted keyset pages of agents: each call yields the next ``limit`` agents after the cursor, or raises."""

    def __init__(self, agents: list[AgentRecord], failures: list[Exception] | None = None, healthy: int = 0) -> None:
        self.agents: list[AgentRecord] = agents
        self.failures: list[Exception] = failures or []
        self.healthy: int = healthy
        self.cursors: list[UUID | None] = []

    async def list_workspace_mirror_page(self, after_id: UUID | None, limit: int) -> list[AgentRecord]:
        self.cursors.append(after_id)
        if self.failures and len(self.cursors) > self.healthy:
            raise self.failures.pop(0)
        return [agent for agent in self.agents if after_id is None or agent.id > after_id][:limit]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP) -> list[float]:
    recorded: list[float] = []

    async def paced(seconds: float) -> None:
        recorded.append(seconds)
        await worker_http.pacing.wait(seconds)

    monkeypatch.setattr(worker, "sleep", paced)
    monkeypatch.setattr(worker, "PAGE_SIZE", 2)
    return recorded


def start(
    monkeypatch: pytest.MonkeyPatch,
    worker_http: WorkerHTTP,
    tmp_path: Path,
    directory: Directory,
    mirrored: list[UUID],
    *,
    store: object | None = object(),
    failures: dict[UUID, Exception] | None = None,
    missing: set[UUID] | None = None,
    mirror_with: Callable[[AgentRecord], Awaitable[ReconcileReport | None]] | None = None,
) -> asyncio.Task[object]:
    """Start the worker against a scripted directory, a fake store and per-agent state directories under tmp_path."""

    async def mirror(agent: AgentRecord) -> ReconcileReport | None:
        mirrored.append(agent.id)
        if failures is not None and agent.id in failures:
            raise failures[agent.id]
        return ReconcileReport()

    def state_dir(agent_id: UUID) -> Path:
        path = tmp_path / str(agent_id)
        if missing is None or agent_id not in missing:
            path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(worker, "agent_dao", directory)
    monkeypatch.setattr(worker, "durable_store", lambda: store)
    monkeypatch.setattr(worker, "agent_state_dir", state_dir)
    monkeypatch.setattr(worker, "mirror_agent", mirror_with or mirror)
    return worker_http.start_loop(
        "workspace_mirror", worker.start_workspace_mirror_worker(), budget_seconds=worker.progress_budget_seconds()
    )


def test_budget_is_one_interval_plus_one_page_read_plus_the_first_agent_to_finish(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "WORKSPACE_MIRROR_INTERVAL_SECONDS", 120.0)

    assert worker.progress_budget_seconds() == 120.0 + worker.PAGE_READ_SECONDS + worker.AGENT_SECONDS
    assert worker.AGENT_SECONDS > PASS_BUDGET_SECONDS


async def test_a_pass_mirrors_every_agent_in_id_order_then_beats_and_sleeps_the_interval(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], tmp_path: Path
):
    # Given five agents and a page size of two.
    agents = _agents(5)
    directory, mirrored = Directory(agents), []
    task = start(monkeypatch, worker_http, tmp_path, directory, mirrored)
    # When the first pass completes and the loop sleeps.
    await worker_http.pacing.paused()
    # Then every agent was mirrored once, page by page in id order, each page resumed after the last id, and the pass slept the interval.
    pages = [agents[0:2], agents[2:4], agents[4:5]]
    assert [set(mirrored[start : start + len(page)]) for start, page in zip((0, 2, 4), pages, strict=True)] == [
        {agent.id for agent in page} for page in pages
    ]
    assert directory.cursors == [None, agents[1].id, agents[3].id]
    assert sleeps == [get_settings().WORKSPACE_MIRROR_INTERVAL_SECONDS]
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)
    # And the next pass starts again from the beginning.
    await worker_http.pacing.step()
    assert directory.cursors[3:] == [None, agents[1].id, agents[3].id]
    assert not task.done()


async def test_without_a_durable_store_the_loop_pages_nothing_but_keeps_beating(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], tmp_path: Path
):
    # Given no durable store, as in local storage mode.
    directory, mirrored = Directory(_agents(3)), []
    task = start(monkeypatch, worker_http, tmp_path, directory, mirrored, store=None)
    await worker_http.pacing.paused()
    # When the clock runs to the edge of the budget and then past it with one more idle pass in between.
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)
    await worker_http.pacing.step(2.0)
    # Then the idle pass beat, so the loop is still ready, and the database was never asked for agents.
    await worker_http.ready(expected=True)
    assert (directory.cursors, mirrored) == ([], [])
    assert sleeps == [get_settings().WORKSPACE_MIRROR_INTERVAL_SECONDS] * 2
    assert not task.done()


async def test_an_agent_without_a_local_directory_is_skipped(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], tmp_path: Path
):
    agents = _agents(3)
    directory, mirrored = Directory(agents), []
    _ = start(monkeypatch, worker_http, tmp_path, directory, mirrored, missing={agents[1].id})

    await worker_http.pacing.paused()

    assert mirrored == [agents[0].id, agents[2].id]


@pytest.mark.parametrize(
    "failure",
    [
        MirrorReconcileError("stopped", had_base=False),
        MirrorReconcileError("stopped", had_base=True),
        OSError("disk"),
        SlotsBusyError(),
    ],
)
async def test_one_agents_failure_is_logged_and_the_pass_still_completes(
    monkeypatch: pytest.MonkeyPatch,
    worker_http: WorkerHTTP,
    sleeps: list[float],
    tmp_path: Path,
    failure: Exception,
):
    # Given the middle agent's mirror pass fails.
    agents = _agents(3)
    directory, mirrored = Directory(agents), []
    task = start(monkeypatch, worker_http, tmp_path, directory, mirrored, failures={agents[1].id: failure})
    # When the pass runs.
    await worker_http.pacing.paused()
    # Then the remaining agents were still mirrored and the completed pass beat.
    assert sorted(mirrored) == sorted(agent.id for agent in agents)
    assert not task.done()
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)


async def test_database_errors_back_off_without_beating_and_resume_from_the_same_cursor(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], tmp_path: Path
):
    # Given two failing page reads before a healthy one.
    agents = _agents(1)
    directory, mirrored = Directory(agents, [DbError("down"), OperationalError("down")]), []
    task = start(monkeypatch, worker_http, tmp_path, directory, mirrored)
    await worker_http.pacing.paused()
    # When the progress budget lapses during the first backoff.
    worker_http.pacing.now += worker.progress_budget_seconds() + 1
    await worker_http.ready(expected=False)
    await worker_http.pacing.step()
    # Then the failures backed off with doubling delays and never beat; the completed pass restores readiness.
    await worker_http.ready(expected=False)
    assert not task.done()
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)
    assert sleeps == [
        worker.DB_RETRY_BASE_SECONDS,
        worker.DB_RETRY_BASE_SECONDS * 2,
        get_settings().WORKSPACE_MIRROR_INTERVAL_SECONDS,
    ]
    assert directory.cursors == [None, None, None]
    assert mirrored == [agents[0].id]
    assert worker.DB_RETRY_CAP_SECONDS < worker.PAGE_READ_SECONDS


async def test_an_empty_directory_still_completes_a_pass(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], tmp_path: Path
):
    directory, mirrored = Directory([]), []
    task = start(monkeypatch, worker_http, tmp_path, directory, mirrored)

    await worker_http.pacing.paused()

    assert (mirrored, directory.cursors) == ([], [None])
    assert sleeps == [get_settings().WORKSPACE_MIRROR_INTERVAL_SECONDS]
    assert not task.done()


def _agents_of(*tenants: UUID) -> list[AgentRecord]:
    return sorted(
        (AgentRecord(id=uuid4(), name="Agent", creator_id=uuid4(), tenant_id=tenant) for tenant in tenants),
        key=lambda agent: agent.id,
    )


class Held:
    """A mirror pass that stays in flight until the test releases that agent; records how many ran at once."""

    def __init__(self, agents: list[AgentRecord]) -> None:
        self.entered: dict[UUID, asyncio.Event] = {agent.id: asyncio.Event() for agent in agents}
        self.release: dict[UUID, asyncio.Event] = {agent.id: asyncio.Event() for agent in agents}
        self.running: int = 0
        self.peak: int = 0

    async def __call__(self, agent: AgentRecord) -> ReconcileReport:
        self.running += 1
        self.peak = max(self.peak, self.running)
        self.entered[agent.id].set()
        try:
            await asyncio.wait_for(self.release[agent.id].wait(), 10)
        finally:
            self.running -= 1
        return ReconcileReport()

    async def entered_all(self, *agents: AgentRecord) -> None:
        for agent in agents:
            await asyncio.wait_for(self.entered[agent.id].wait(), 10)

    def release_all(self) -> None:
        for event in self.release.values():
            event.set()


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_agents_of_different_tenants_mirror_at_once_up_to_the_configured_number(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], tmp_path: Path
):
    # Given three agents of three tenants and a limit of two at once.
    monkeypatch.setattr(worker, "PAGE_SIZE", 10)
    monkeypatch.setattr(get_settings(), "WORKSPACE_MIRROR_CONCURRENCY", 2)
    agents = _agents_of(uuid4(), uuid4(), uuid4())
    held = Held(agents)
    _ = start(monkeypatch, worker_http, tmp_path, Directory(agents), [], mirror_with=held)
    # When the first two are in flight.
    await held.entered_all(agents[0], agents[1])
    await _settle()
    # Then the third waits for a free slot.
    assert (held.peak, held.entered[agents[2].id].is_set()) == (2, False)
    held.release[agents[0].id].set()
    await held.entered_all(agents[2])
    held.release_all()
    await worker_http.pacing.paused()
    assert held.peak == 2


async def test_two_agents_of_one_tenant_never_run_together_and_the_next_tenant_is_not_blocked(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], tmp_path: Path
):
    # Given two agents of one tenant listed before an agent of another, and room for three at once.
    monkeypatch.setattr(worker, "PAGE_SIZE", 10)
    monkeypatch.setattr(get_settings(), "WORKSPACE_MIRROR_CONCURRENCY", 3)
    shared = uuid4()
    agents = _agents_of(shared, shared, uuid4())
    first, second = [agent for agent in agents if agent.tenant_id == shared]
    other = next(agent for agent in agents if agent.tenant_id != shared)
    held = Held(agents)
    _ = start(monkeypatch, worker_http, tmp_path, Directory(agents), [], mirror_with=held)
    # When the first of the tenant and the other tenant's agent are in flight.
    await held.entered_all(first, other)
    await _settle()
    # Then the tenant's second agent waits for its sibling and starts as soon as it finishes.
    assert (held.entered[second.id].is_set(), held.peak) == (False, 2)
    held.release[first.id].set()
    await held.entered_all(second)
    held.release_all()
    await worker_http.pacing.paused()
    assert held.peak == 2


async def test_a_failing_agent_frees_its_tenant_and_every_agent_is_still_mirrored(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], tmp_path: Path
):
    monkeypatch.setattr(worker, "PAGE_SIZE", 10)
    shared = uuid4()
    agents = _agents_of(shared, shared, uuid4())
    directory, mirrored = Directory(agents), []
    _ = start(monkeypatch, worker_http, tmp_path, directory, mirrored, failures={agents[0].id: OSError("disk")})

    await worker_http.pacing.paused()

    assert sorted(mirrored) == sorted(agent.id for agent in agents)
