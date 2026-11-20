import asyncio
from collections.abc import Callable, Sequence
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError

from app.config import get_settings
from app.db.errors import DbError
from app.records.agent import AgentRecord
from app.services import agent_resource_pressure_worker as worker
from app.services.agent_resource_pressure import GuestReads
from app.services.agent_resource_pressure_docker import DOCKER_TIMEOUT_SECONDS
from app.services.agent_resource_pressure_events import OomHandler
from worker_progress_fixtures import Pacing, WorkerHTTP, worker_http

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

    async def list_workspace_quota_page(self, after_id: UUID | None, limit: int) -> list[AgentRecord]:
        self.cursors.append(after_id)
        if self.failures and len(self.cursors) > self.healthy:
            raise self.failures.pop(0)
        return [agent for agent in self.agents if after_id is None or agent.id > after_id][:limit]


def _no_time(_page: int) -> float:
    return 0.0


class Sampler:
    """Records the pages sampled and the orphan sweeps; a numbered page can spend paced-clock time, park, latch or fail.

    ``spend`` maps a page number (counted across passes) to the seconds the paced clock moves while that page runs.
    """

    def __init__(
        self,
        fail_page: int | None = None,
        fail_sweep: bool = False,
        *,
        pacing: Pacing | None = None,
        spend: Callable[[int], float] = _no_time,
        park_on: int | None = None,
        latch_on: int | None = None,
    ) -> None:
        self.pages: list[list[UUID]] = []
        self.reads: list[GuestReads] = []
        self.sweeps: int = 0
        self.fail_page: int | None = fail_page
        self.fail_sweep: bool = fail_sweep
        self.pacing: Pacing | None = pacing
        self.spend: Callable[[int], float] = spend
        self.park_on: int | None = park_on
        self.latch_on: int | None = latch_on

    async def sample_page(self, agents: Sequence[AgentRecord], reads: GuestReads) -> None:
        self.pages.append([agent.id for agent in agents])
        self.reads.append(reads)
        number = len(self.pages)
        if self.pacing is not None:
            self.pacing.now += self.spend(number)
            if self.park_on == number:
                await self.pacing.wait(0.0)
        if self.latch_on == number:
            reads.unavailable = True
        if self.fail_page == number:
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


def test_budget_is_one_interval_plus_the_per_page_bounds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS", 1800.0)

    assert worker.progress_budget_seconds() == (
        1800.0
        + worker.ORPHAN_SWEEP_SECONDS
        + worker.PAGE_READ_SECONDS
        + worker.PAGE_DOCKER_SECONDS
        + worker.PAGE_DATABASE_SECONDS
    )
    assert worker.PAGE_DOCKER_SECONDS == 2 * DOCKER_TIMEOUT_SECONDS


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


async def test_every_page_of_a_pass_shares_one_latch_and_the_next_pass_starts_with_a_fresh_one(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given a pass whose first page latches Docker as unavailable.
    sampler = Sampler(latch_on=1)
    _ = start(monkeypatch, worker_http, Directory(_agents(5)), sampler)
    await worker_http.pacing.paused()
    # Then all three pages of the pass took the same, latched state.
    assert len(sampler.reads) == 3
    assert all(reads is sampler.reads[0] for reads in sampler.reads)
    assert sampler.reads[0].unavailable
    # When the next pass runs.
    await worker_http.pacing.step()
    # Then its pages share a new state that asks Docker again.
    assert len(sampler.reads) == 6
    assert all(reads is sampler.reads[3] for reads in sampler.reads[3:])
    assert sampler.reads[3] is not sampler.reads[0]
    assert not sampler.reads[3].unavailable


async def test_pages_that_each_spend_the_full_docker_timeout_outlast_the_old_flat_allowance_and_stay_ready(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given the retired budget (the interval plus a flat hour) and a pass whose every page spends a full Docker timeout.
    retired_budget = get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS + 3600.0
    pages = int(retired_budget // DOCKER_TIMEOUT_SECONDS) + 1
    sampler = Sampler(pacing=worker_http.pacing, spend=lambda _page: DOCKER_TIMEOUT_SECONDS, park_on=pages)
    _ = start(monkeypatch, worker_http, Directory(_agents(2 * pages - 1)), sampler)
    # When the last page has spent its time, so the pass has outlasted the retired budget.
    await worker_http.pacing.paused()
    # Then the loop is still ready, because every earlier page beat.
    assert len(sampler.pages) == pages
    assert worker_http.pacing.now > retired_budget
    await worker_http.ready(expected=True)
    # And the pass then completes.
    await worker_http.pacing.step(0.0)
    assert sleeps == [get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS]


async def test_a_failed_page_still_beats_and_a_page_held_past_the_budget_turns_the_loop_stale(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given a first page that runs a long time and fails, and a second page that never finishes.
    sampler = Sampler(
        fail_page=1, pacing=worker_http.pacing, spend=lambda page: 1000.0 if page == 1 else 0.0, park_on=2
    )
    task = start(monkeypatch, worker_http, Directory(_agents(4)), sampler)
    await worker_http.pacing.paused()
    # Then the failed page's beat is what the second page is measured from.
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)
    # And once the held page passes the budget the loop turns stale without being cancelled.
    worker_http.pacing.now += 2
    await worker_http.ready(expected=False)
    assert not task.done()


async def test_a_database_retry_after_a_completed_page_never_beats(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given a first page that beat, then two failing page reads before a healthy one.
    directory = Directory(_agents(3), [DbError("down"), OperationalError("down")], healthy=1)
    sampler = Sampler(pacing=worker_http.pacing, spend=lambda page: 1000.0 if page == 1 else 0.0)
    _ = start(monkeypatch, worker_http, directory, sampler)
    await worker_http.pacing.paused()
    # When the retries run past the budget of the first page's beat.
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)
    await worker_http.pacing.step(2.0)
    # Then the retries did not beat, and the next completed page does.
    await worker_http.ready(expected=False)
    await worker_http.pacing.step(0.0)
    await worker_http.ready(expected=True)
    assert sleeps == [
        worker.DB_RETRY_BASE_SECONDS,
        worker.DB_RETRY_BASE_SECONDS * 2,
        get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS,
    ]


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
    assert worker.DB_RETRY_CAP_SECONDS < worker.PAGE_READ_SECONDS


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
