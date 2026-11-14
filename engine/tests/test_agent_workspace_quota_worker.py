import asyncio
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError

from app.config import get_settings
from app.db.errors import DbError
from app.records.agent import AgentRecord
from app.services import agent_workspace_quota_worker as worker
from app.services.agent_workspace_quota import LayerReads
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
    measured: list[UUID],
    *,
    fail_on: UUID | None = None,
    layer_reads: list[LayerReads] | None = None,
) -> asyncio.Task[object]:
    async def measure(agent: AgentRecord, layers: LayerReads) -> None:
        measured.append(agent.id)
        if layer_reads is not None:
            layer_reads.append(layers)
        if agent.id == fail_on:
            raise OSError("walk failed")

    monkeypatch.setattr(worker, "agent_dao", directory)
    monkeypatch.setattr(worker, "measure_agent", measure)
    return worker_http.start_loop(
        "agent_workspace_quota",
        worker.start_agent_workspace_quota_worker(),
        budget_seconds=worker.progress_budget_seconds(),
    )


def test_budget_is_one_interval_plus_the_pass_allowance(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS", 1800.0)

    assert worker.progress_budget_seconds() == 1800.0 + worker.PASS_ALLOWANCE_SECONDS


async def test_a_pass_pages_every_agent_in_id_order_then_beats_and_sleeps_the_interval(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given five agents and a page size of two.
    agents = _agents(5)
    directory, measured = Directory(agents), []
    task = start(monkeypatch, worker_http, directory, measured)
    # When the first pass completes and the loop sleeps.
    await worker_http.pacing.paused()
    # Then every agent was measured once in id order, each page resumed after the last id, and the pass slept the interval.
    assert measured == [agent.id for agent in agents]
    assert directory.cursors == [None, agents[1].id, agents[3].id]
    assert sleeps == [get_settings().AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS]
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)
    # And the next pass starts again from the beginning.
    await worker_http.pacing.step()
    assert directory.cursors[3:] == [None, agents[1].id, agents[3].id]
    assert not task.done()


async def test_every_agent_of_a_pass_shares_one_layer_read_state_and_the_next_pass_starts_fresh(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    agents = _agents(5)
    directory, measured, seen = Directory(agents), [], []
    _ = start(monkeypatch, worker_http, directory, measured, layer_reads=seen)
    await worker_http.pacing.paused()

    assert len(seen) == 5
    assert all(layers is seen[0] for layers in seen)

    await worker_http.pacing.step()

    assert len(seen) == 10
    assert all(layers is seen[5] for layers in seen[5:])
    assert seen[5] is not seen[0]
    assert not seen[5].unavailable


async def test_one_agents_failure_is_logged_and_the_pass_still_completes(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given the middle agent's measurement fails.
    agents = _agents(3)
    directory, measured = Directory(agents), []
    task = start(monkeypatch, worker_http, directory, measured, fail_on=agents[1].id)
    # When the pass runs.
    await worker_http.pacing.paused()
    # Then the remaining agents were still measured and the completed pass beat.
    assert measured == [agent.id for agent in agents]
    assert not task.done()
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)


async def test_database_errors_back_off_without_beating_and_resume_from_the_same_cursor(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    # Given two failing page reads before a healthy one.
    agents = _agents(1)
    directory, measured = Directory(agents, [DbError("down"), OperationalError("down")]), []
    task = start(monkeypatch, worker_http, directory, measured)
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
        get_settings().AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS,
    ]
    assert directory.cursors == [None, None, None]
    assert measured == [agents[0].id]
    assert worker.DB_RETRY_CAP_SECONDS < worker.PASS_ALLOWANCE_SECONDS


async def test_an_empty_directory_still_completes_a_pass(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
):
    directory, measured = Directory([]), []
    task = start(monkeypatch, worker_http, directory, measured)

    await worker_http.pacing.paused()

    assert (measured, directory.cursors) == ([], [None])
    assert sleeps == [get_settings().AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS]
    assert not task.done()
