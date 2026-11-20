import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError

from app.config import get_settings
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
    monkeypatch.setattr(worker, "mirror_agent", mirror)
    return worker_http.start_loop(
        "workspace_mirror", worker.start_workspace_mirror_worker(), budget_seconds=worker.progress_budget_seconds()
    )


def test_budget_is_one_interval_plus_one_page_of_agents_each_at_their_bound(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "WORKSPACE_MIRROR_INTERVAL_SECONDS", 120.0)

    assert (
        worker.progress_budget_seconds() == 120.0 + worker.PAGE_READ_SECONDS + worker.PAGE_SIZE * worker.AGENT_SECONDS
    )
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
    # Then every agent was mirrored once in id order, each page resumed after the last id, and the pass slept the interval.
    assert mirrored == [agent.id for agent in agents]
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
    [MirrorReconcileError("stopped", had_base=False), MirrorReconcileError("stopped", had_base=True), OSError("disk")],
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
    assert mirrored == [agent.id for agent in agents]
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
