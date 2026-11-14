import asyncio
from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError

from app.config import get_settings
from app.db.errors import DbError
from app.records.agent import AgentRecord
from app.services import agent_resource_pressure_worker as worker
from app.services.agent_resource_pressure_events import OomHandler
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

    def __init__(self, agents: list[AgentRecord], failures: list[Exception] | None = None) -> None:
        self.agents: list[AgentRecord] = agents
        self.failures: list[Exception] = failures or []
        self.cursors: list[UUID | None] = []

    async def list_workspace_quota_page(self, after_id: UUID | None, limit: int) -> list[AgentRecord]:
        self.cursors.append(after_id)
        if self.failures:
            raise self.failures.pop(0)
        return [agent for agent in self.agents if after_id is None or agent.id > after_id][:limit]


class Sampler:
    """Records the pages sampled and the orphan sweeps, failing on a chosen page or sweep."""

    def __init__(self, fail_page: int | None = None, fail_sweep: bool = False) -> None:
        self.pages: list[list[UUID]] = []
        self.sweeps: int = 0
        self.fail_page: int | None = fail_page
        self.fail_sweep: bool = fail_sweep

    async def sample_page(self, agents: Sequence[AgentRecord]) -> None:
        self.pages.append([agent.id for agent in agents])
        if self.fail_page == len(self.pages):
            raise OSError("docker went away")

    async def close_orphans(self) -> int:
        self.sweeps += 1
        if self.fail_sweep:
            raise OSError("database went away")
        return 0


class Reader:
    """Stands in for the docker events reader: parks until cancelled and records its life."""

    def __init__(self) -> None:
        self.handlers: list[OomHandler] = []
        self.started: asyncio.Event = asyncio.Event()
        self.cancelled: bool = False

    async def run(self, handle: OomHandler) -> None:
        self.handlers.append(handle)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


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
    directory: Directory,
    sampler: Sampler,
    reader: Reader | None = None,
) -> asyncio.Task[object]:
    monkeypatch.setattr(worker, "agent_dao", directory)
    monkeypatch.setattr(worker, "sample_page", sampler.sample_page)
    monkeypatch.setattr(worker, "close_orphaned_episodes", sampler.close_orphans)
    monkeypatch.setattr(worker, "read_oom_events", (reader or Reader()).run)
    return worker_http.start_loop(
        "agent_resource_pressure",
        worker.start_agent_resource_pressure_worker(),
        budget_seconds=worker.progress_budget_seconds(),
    )


def test_the_default_interval_is_five_minutes_and_the_threshold_ninety_percent():
    settings = get_settings()

    assert settings.AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS == 300.0
    assert settings.AGENT_RESOURCE_PRESSURE_THRESHOLD == 0.9


def test_budget_is_one_interval_plus_the_pass_allowance(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS", 1800.0)

    assert worker.progress_budget_seconds() == 1800.0 + worker.PASS_ALLOWANCE_SECONDS


async def test_a_pass_sweeps_orphans_then_samples_every_page_in_id_order_beats_and_sleeps_the_interval(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given five agents and a page size of two.
    agents = _agents(5)
    directory, sampler = Directory(agents), Sampler()
    task = start(monkeypatch, worker_http, directory, sampler)
    # When the first pass completes and the loop sleeps.
    await worker_http.pacing.paused()
    # Then every page was sampled once in id order after one orphan sweep, and the pass slept the interval.
    assert sampler.pages == [[agent.id for agent in agents[0:2]], [agent.id for agent in agents[2:4]], [agents[4].id]]
    assert sampler.sweeps == 1
    assert directory.cursors == [None, agents[1].id, agents[3].id]
    assert sleeps == [get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS]
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)
    # And the next pass sweeps again and starts from the beginning.
    await worker_http.pacing.step()
    assert sampler.sweeps == 2
    assert directory.cursors[3:] == [None, agents[1].id, agents[3].id]
    assert not task.done()


async def test_one_pages_failure_is_logged_and_the_pass_still_completes(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given the middle page's sampling fails.
    agents = _agents(5)
    directory, sampler = Directory(agents), Sampler(fail_page=2)
    task = start(monkeypatch, worker_http, directory, sampler)
    # When the pass runs.
    await worker_http.pacing.paused()
    # Then the other pages were still sampled and the completed pass beat.
    assert len(sampler.pages) == 3
    assert not task.done()
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)


async def test_a_failed_orphan_sweep_does_not_stop_the_pass(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    agents = _agents(3)
    directory, sampler = Directory(agents), Sampler(fail_sweep=True)
    task = start(monkeypatch, worker_http, directory, sampler)

    await worker_http.pacing.paused()

    assert (sampler.sweeps, len(sampler.pages)) == (1, 2)
    assert not task.done()
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)


async def test_database_errors_back_off_without_beating_and_resume_from_the_same_cursor(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given two failing page reads before a healthy one.
    agents = _agents(1)
    directory, sampler = Directory(agents, [DbError("down"), OperationalError("down")]), Sampler()
    task = start(monkeypatch, worker_http, directory, sampler)
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
        get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS,
    ]
    assert directory.cursors == [None, None, None]
    assert sampler.pages == [[agents[0].id]]
    assert sampler.sweeps == 1
    assert worker.DB_RETRY_CAP_SECONDS < worker.PASS_ALLOWANCE_SECONDS


async def test_an_empty_directory_still_completes_a_pass(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    directory, sampler = Directory([]), Sampler()
    task = start(monkeypatch, worker_http, directory, sampler)

    await worker_http.pacing.paused()

    assert (sampler.pages, directory.cursors) == ([[]], [None])
    assert sleeps == [get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS]
    assert not task.done()


async def test_the_oom_reader_starts_with_the_worker_and_is_cancelled_with_it(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given a worker whose sampling loop is parked between passes beside a parked reader.
    reader = Reader()
    task = start(monkeypatch, worker_http, Directory(_agents(1)), Sampler(), reader)
    await asyncio.wait_for(reader.started.wait(), 2)
    await worker_http.pacing.paused()
    # Then the reader runs with the event recorder and keeps running through a completed pass.
    assert reader.handlers == [worker.record_oom_event]
    assert not reader.cancelled
    # When the runtime shuts the worker down.
    await worker_http.runtime.tasks.stop()
    # Then the reader is cancelled with it.
    assert reader.cancelled
    assert task.cancelled()
