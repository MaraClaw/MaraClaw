import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import monotonic
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi import FastAPI
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_whatsapp_personal_integration import connected

from app.api.whatsapp import router
from app.config import get_settings
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.personal_bridge_fleet import FleetFullError
from app.dao.whatsapp_personal_dao import claim, get_personal
from app.db import session as db_session
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels import whatsapp_personal_child as child_module, whatsapp_personal_supervisor as supervisor
from app.services.channels.whatsapp_personal_child import PersonalChild
from app.services.channels.whatsapp_personal_control import start
from worker_progress_fixtures import Pacing, WorkerHTTP, worker_http

__all__ = ["channel_db", "receipt_db", "worker_http"]


def use_pool(f, monkeypatch):
    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield f.db.first.raw

    monkeypatch.setattr(db_session, "get_pool", lambda: Pool())


@asynccontextmanager
async def personal_pool(f):
    await init_pool(conninfo=f.db.conninfo, min_size=1, max_size=4)
    try:
        yield
    finally:
        await close_pool()


async def test_public_personal_api_runs_fresh_generation_contract(channel_db, monkeypatch):
    # Given the real router backed by isolated PostgreSQL.
    f = channel_db
    use_pool(f, monkeypatch)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: f.db.user
    path = f"/api/agents/{f.config.agent_id}/whatsapp-channel"
    # When a creator starts and unlinks through HTTP.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as client:
        initial = await client.get(path + "/personal")
        response = await client.post(path + "/personal/start", json={"expected_generation": None})
        configured = await client.get(path)
        generation = response.json()["generation"]
        stale = await client.post(path + "/personal/start", json={"expected_generation": None})
        removed = await client.delete(path + "/personal", params={"expected_generation": generation})
    # Then status, mode, stale rejection and privacy headers match the frozen API.
    assert initial.status_code == 200
    assert initial.json()["generation"] is None
    assert response.status_code == 202
    assert response.json()["state"] == "pending"
    assert response.headers["cache-control"] == "no-store, private"
    assert configured.status_code == 200
    assert configured.json()["mode"] == "personal_qr"
    assert configured.json()["app_secret"] is None
    assert stale.status_code == 409
    assert removed.status_code == 200
    assert removed.json()["state"] == "disconnected"


async def test_supervisor_private_pipe_auth_ack_and_exact_reap(channel_db):
    # Given a real child process speaking only the frozen JSONL protocol.
    f = channel_db
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        owner = await claim(f.config.id)
        assert owner is not None
    script = """
import json,sys
init=json.loads(sys.stdin.readline())
token=init['token']
def emit(value):
 value.update(token=token)
 print(json.dumps(value),flush=True)
def result(ident):
 while True:
  frame=json.loads(sys.stdin.readline())
  if frame['type']=='result' and frame['id']==ident:
   assert frame['ok'] is True
   return frame['value']
emit({'type':'auth_set','id':'set','batches':[{'category':'arbitrary-key','values':{'one':{'type':'Buffer','data':[1,2]}}}]})
result('set')
emit({'type':'auth_get','id':'get','category':'arbitrary-key','ids':['one','missing']})
assert result('get')=={'one':{'type':'Buffer','data':[1,2]}}
emit({'type':'creds_set','id':'creds','creds':'{"registered":true}'})
result('creds')
emit({'type':'identity','id':'identity','account_id':'15550001@s.whatsapp.net','pn_jid':'15550001@s.whatsapp.net','lid_jid':None})
emit({'type':'closed','id':'closed','reason':'shutdown'})
"""
    child = PersonalChild(owner)
    # When auth mutations are acknowledged over inherited pipes.
    async with personal_pool(f):
        with anyio.fail_after(10):
            await child.run((sys.executable, "-u", "-c", script))
    # Then ACKs reflect committed encrypted storage and the exact child exited.
    assert child.process is not None
    assert child.process.returncode == 0
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.claim_token is None
        assert row.account_id == "15550001@s.whatsapp.net"
        assert await f.db.second.fetchval("SELECT count(*) FROM whatsapp_personal_auth") == 2


async def test_lifecycle_stop_revokes_route_and_owner_change_purges(channel_db):
    # Given encrypted personal auth on a live claimed account.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        agent = await agent_dao.get(owner.agent_id)
        assert agent is not None
    # When the existing state owner stops the agent.
    async with transaction(f.db.first):
        stopped = await agent_dao.update(db_obj=agent, obj_in={"status": "stopped"})
    # Then logical routing is revoked without pretending physical death.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.generation != owner.generation
        assert row.state == "disconnecting"
        assert row.account_id == owner.account_id
        assert not row.purge_requested
    async with transaction(f.db.first):
        await agent_dao.update(db_obj=stopped, obj_in={"creator_id": f.sender_id})
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.purge_requested
        assert row.account_id == owner.account_id


def pending_connection() -> PersonalConnection:
    return PersonalConnection(
        config_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        owner_id=uuid4(),
        auth_epoch=uuid4(),
        generation=uuid4(),
        attempt_id=uuid4(),
        revision=1,
        state="pending",
        desired=True,
        created_at=datetime.now(UTC),
    )


async def stop_supervisor(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def forever() -> None:
    await asyncio.Event().wait()


async def explodes() -> None:
    raise RuntimeError("child exploded")


def lives_until(release: asyncio.Event, *, then_explodes: bool) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        await release.wait()
        if then_explodes:
            await explodes()

    return run


class Scripted:
    """A connection whose bridge child plays the next scripted behaviour per spawn, then stays up."""

    def __init__(
        self,
        pacing: Pacing,
        *script: Callable[[], Awaitable[None]],
        failing_stage: str = "run_child",
        declines: bool = False,
        hangs: bool = False,
        refused: FleetFullError | None = None,
    ) -> None:
        self.connection = pending_connection()
        self.pacing = pacing
        self.script = list(script)
        self.failing_stage = failing_stage
        self.declines = declines
        self.hangs = hangs
        self.refused = refused
        self.attempts: list[float] = []
        self.spawns: list[float] = []

    async def play(self) -> None:
        await (self.script.pop(0) if self.script else forever)()

    async def claim(self) -> PersonalConnection | None:
        self.attempts.append(self.pacing.now)
        if self.hangs:
            await forever()
        if self.refused is not None:
            raise self.refused
        if self.declines:
            return None
        self.spawns.append(self.pacing.now)
        if self.failing_stage == "claim":
            await self.play()
        return self.connection

    async def run_child(self) -> None:
        if self.failing_stage == "run_child":
            await self.play()
        else:
            await forever()


def supervise(
    monkeypatch: pytest.MonkeyPatch, pacing: Pacing, *children: Scripted, value: float = 0.0
) -> tuple[asyncio.Task[None], AsyncMock]:
    by_id = {child.connection.config_id: child for child in children}
    candidates = AsyncMock(return_value=[child.connection for child in children])

    async def claim(config_id):
        return await by_id[config_id].claim()

    async def run_child(connection, _command):
        await by_id[connection.config_id].run_child()

    monkeypatch.setattr(supervisor, "candidates", candidates)
    monkeypatch.setattr(supervisor, "claim", claim)
    monkeypatch.setattr(supervisor, "run_child", run_child)
    monkeypatch.setattr(supervisor.anyio, "sleep", pacing.wait)
    return asyncio.create_task(supervisor.personal_supervisor(pacing.clock, lambda: value)), candidates


@pytest.mark.parametrize(
    ("value", "failing_stage", "cooldowns"),
    [
        (0.0, "run_child", [30, 60, 120, 150, 150]),
        (1.0, "run_child", [60, 120, 240, 300, 300]),
        (0.0, "claim", [30, 60]),
    ],
)
async def test_supervisor_escalates_a_failing_connections_cooldown_and_leaves_its_sibling_alone(
    monkeypatch, value, failing_stage, cooldowns
):
    # Given one connection that raises an unclassified error every time and a healthy sibling.
    pacing, fail_connection = Pacing(), AsyncMock()
    monkeypatch.setattr(child_module, "fail_connection", fail_connection)
    bad = Scripted(pacing, *[explodes] * 8, failing_stage=failing_stage)
    good = Scripted(pacing)
    task, _ = supervise(monkeypatch, pacing, bad, good, value=value)
    await pacing.paused()
    assert bad.spawns == [0.0]
    # When the clock runs through each cooldown.
    for respawns, cooldown in enumerate(cooldowns, start=1):
        await pacing.step(cooldown - 1)
        # Then the connection stays down until its cooldown ends, and the next cooldown is longer.
        assert len(bad.spawns) == respawns
        await pacing.step(1)
        assert len(bad.spawns) == respawns + 1
    assert not task.done()
    assert len(good.spawns) == 1
    fail_connection.assert_not_awaited()
    await stop_supervisor(task)


@pytest.mark.parametrize(
    ("run_seconds", "cooldown"),
    [(supervisor._HEALTHY_CHILD_SECONDS - 1, 60), (supervisor._HEALTHY_CHILD_SECONDS, 30)],
)
async def test_supervisor_restarts_escalation_only_after_a_healthy_child_run(monkeypatch, run_seconds, cooldown):
    # Given a connection that fails fast once and then fails again after running for run_seconds.
    pacing, release = Pacing(), asyncio.Event()
    bad = Scripted(pacing, explodes, lives_until(release, then_explodes=True))
    task, _ = supervise(monkeypatch, pacing, bad)
    await pacing.paused()
    await pacing.step(30)
    assert len(bad.spawns) == 2
    await pacing.step(run_seconds)
    release.set()
    await pacing.step(0)
    assert len(bad.spawns) == 2
    # When the clock runs through the cooldown that failure earned.
    await pacing.step(cooldown - 1)
    assert len(bad.spawns) == 2
    await pacing.step(1)
    # Then only a healthy run earned the first cooldown again, and a shorter one the second.
    assert len(bad.spawns) == 3
    await stop_supervisor(task)


async def test_supervisor_clears_failures_when_a_healthy_child_exits_cleanly(monkeypatch):
    # Given a connection that fails fast, then runs a healthy stretch and exits cleanly, then fails again.
    pacing, release = Pacing(), asyncio.Event()
    bad = Scripted(pacing, explodes, lives_until(release, then_explodes=False), explodes)
    task, _ = supervise(monkeypatch, pacing, bad)
    await pacing.paused()
    await pacing.step(30)
    await pacing.step(supervisor._HEALTHY_CHILD_SECONDS)
    release.set()
    # When the clean exit is respawned at once and fails.
    await pacing.step(0)
    assert len(bad.spawns) == 3
    # Then that failure earns the first cooldown again.
    await pacing.step(29)
    assert len(bad.spawns) == 3
    await pacing.step(1)
    assert len(bad.spawns) == 4
    await stop_supervisor(task)


async def test_supervisor_drops_a_connections_cooldown_and_failures_when_it_leaves_the_candidates(monkeypatch):
    # Given a connection that fails every time and then stops being a candidate for one pass.
    pacing = Pacing()
    bad = Scripted(pacing, *[explodes] * 4)
    task, candidates = supervise(monkeypatch, pacing, bad)
    await pacing.paused()
    candidates.return_value = []
    await pacing.step(1)
    # When it comes back as a candidate.
    candidates.return_value = [bad.connection]
    await pacing.step(1)
    # Then it is not held back by its old cooldown, and its escalation has started over.
    assert bad.spawns == [0.0, 2.0]
    await pacing.step(29)
    assert len(bad.spawns) == 2
    await pacing.step(1)
    assert bad.spawns == [0.0, 2.0, 32.0]
    await stop_supervisor(task)


async def test_supervisor_cancellation_ends_at_once_while_a_cooldown_is_pending(monkeypatch):
    # Given a failed connection that is cooling down.
    pacing = Pacing()
    bad = Scripted(pacing, explodes)
    task, _ = supervise(monkeypatch, pacing, bad)
    await pacing.paused()
    # When the supervisor is cancelled.
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=2)
    # Then it ends at once, cancelled, without waiting out the cooldown.
    assert task in done
    assert task.cancelled()
    assert bad.spawns == [0.0]


def fleet(monkeypatch: pytest.MonkeyPatch, *, ceiling: int, per_poll: int) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "WHATSAPP_PERSONAL_MAX_RUNNING_CHILDREN", ceiling)
    monkeypatch.setattr(settings, "WHATSAPP_PERSONAL_MAX_STARTS_PER_POLL", per_poll)


def started(children: Sequence[Scripted]) -> list[int]:
    return [index for index, child in enumerate(children) if child.spawns]


def abandoned_claims(logger: Mock) -> list[tuple[object, ...]]:
    return [call.args[1:] for call in logger.warning.call_args_list if "claim_abandoned" in call.args[0]]


async def test_supervisor_brings_a_fleet_back_in_paced_waves_under_the_ceiling(monkeypatch):
    # Given seven waiting connections, oldest first, a ceiling of five running children and two starts per poll.
    fleet(monkeypatch, ceiling=5, per_poll=2)
    pacing, first_exit, logger = Pacing(), asyncio.Event(), Mock()
    monkeypatch.setattr(supervisor, "logger", logger)
    children = [Scripted(pacing, lives_until(first_exit, then_explodes=False))]
    children += [Scripted(pacing) for _ in range(6)]
    task, candidates = supervise(monkeypatch, pacing, *children)
    await pacing.paused()
    # When polls pass.
    waves = [started(children)]
    for _ in range(2):
        await pacing.step(1)
        waves.append(started(children))
    # Then the oldest start first, two per poll, until the ceiling is reached.
    assert waves == [[0, 1], [0, 1, 2, 3], [0, 1, 2, 3, 4]]
    assert [child.spawns for child in children[:5]] == [[0.0], [0.0], [1.0], [1.0], [2.0]]
    # And later polls start nothing while the ceiling holds, reporting that once.
    for _ in range(3):
        await pacing.step(1)
    assert started(children) == [0, 1, 2, 3, 4]
    assert logger.warning.call_count == 1
    # When a child ends and leaves the candidates.
    first_exit.set()
    candidates.return_value = [child.connection for child in children[1:]]
    await pacing.step(1)
    # Then exactly one waiting connection, the oldest, takes the free slot.
    assert started(children) == [0, 1, 2, 3, 4, 5]
    await pacing.step(1)
    assert children[6].spawns == []
    await stop_supervisor(task)


async def test_supervisor_skips_a_cooling_down_connection_without_spending_the_start_pace(monkeypatch):
    # Given the oldest connection fails at once and two healthy ones wait behind it, with one start per poll.
    fleet(monkeypatch, ceiling=10, per_poll=1)
    pacing, fail_connection = Pacing(), AsyncMock()
    monkeypatch.setattr(child_module, "fail_connection", fail_connection)
    bad, first, second = Scripted(pacing, explodes), Scripted(pacing), Scripted(pacing)
    task, _ = supervise(monkeypatch, pacing, bad, first, second)
    await pacing.paused()
    # When polls pass.
    await pacing.step(1)
    await pacing.step(1)
    # Then each poll starts the next connection in line while the failed one cools down.
    assert (bad.spawns, first.spawns, second.spawns) == ([0.0], [1.0], [2.0])
    await pacing.step(27)
    assert bad.spawns == [0.0]
    await pacing.step(1)
    assert bad.spawns == [0.0, 30.0]
    fail_connection.assert_not_awaited()
    await stop_supervisor(task)


async def test_supervisor_does_not_spend_the_start_pace_on_a_declined_claim(monkeypatch):
    # Given two oldest connections whose claims are declined, three healthy ones behind them and two starts per poll.
    fleet(monkeypatch, ceiling=10, per_poll=2)
    pacing = Pacing()
    declined = [Scripted(pacing, declines=True) for _ in range(2)]
    healthy = [Scripted(pacing) for _ in range(3)]
    task, _ = supervise(monkeypatch, pacing, *declined, *healthy)
    await pacing.paused()
    # When polls pass.
    await pacing.step(1)
    # Then the declined claims are tried again every poll and the healthy connections still start two per poll.
    assert [child.attempts for child in declined] == [[0.0, 1.0], [0.0, 1.0]]
    assert [child.spawns for child in healthy] == [[0.0], [0.0], [1.0]]
    await stop_supervisor(task)


async def test_supervisor_ends_the_polls_claims_when_the_fleet_is_full_and_reports_each_episode_once(monkeypatch):
    # Given three waiting connections with room locally, a fourth not yet waiting, and a fleet other processes filled.
    fleet(monkeypatch, ceiling=10, per_poll=10)
    pacing, logger = Pacing(), Mock()
    monkeypatch.setattr(supervisor, "logger", logger)
    full = FleetFullError(live=10, ceiling=10)
    children = [Scripted(pacing, refused=full) for _ in range(3)]
    late = Scripted(pacing, refused=full)
    task, candidates = supervise(monkeypatch, pacing, *children, late)
    candidates.return_value = [child.connection for child in children]
    await pacing.paused()
    # When polls pass.
    for _ in range(3):
        await pacing.step(1)
    # Then each poll tries only the oldest connection, nothing starts, and the fleet-wide count is reported once.
    assert [child.attempts for child in children] == [[0.0, 1.0, 2.0, 3.0], [], []]
    assert started(children) == []
    assert logger.warning.call_count == 1
    assert "fleet_ceiling_reached" in logger.warning.call_args.args[0]
    assert logger.warning.call_args.args[1:] == (10, 10, 3)
    # When room opens.
    for child in children:
        child.refused = None
    await pacing.step(1)
    # Then the oldest connections start in order within that one poll.
    assert started(children) == [0, 1, 2]
    # And a later full fleet is a new episode, reported again.
    candidates.return_value = [child.connection for child in (*children, late)]
    await pacing.step(1)
    assert late.attempts == [5.0]
    assert logger.warning.call_count == 2
    assert logger.warning.call_args.args[1:] == (10, 10, 1)
    await stop_supervisor(task)


async def test_supervisor_abandons_a_hung_claim_and_keeps_polling_and_beating(monkeypatch):
    # Given a claim that never returns for the oldest connection, a healthy one behind it, and a small claim budget.
    monkeypatch.setattr(supervisor, "_CLAIM_TIMEOUT_SECONDS", 0.05)
    progress = Mock()
    monkeypatch.setattr(supervisor, "loop_progress", lambda: progress)
    pacing = Pacing()
    hung, healthy = Scripted(pacing, hangs=True), Scripted(pacing)
    task, _ = supervise(monkeypatch, pacing, hung, healthy)
    # When the first pass runs.
    await pacing.paused()
    # Then the abandoned claim ended that poll's claims and the pass beat.
    assert (hung.attempts, healthy.attempts) == ([0.0], [])
    assert progress.beat.call_count == 1
    # And the next poll, with the abandoned connection cooling down, starts the healthy one.
    await pacing.step(1)
    assert (hung.attempts, healthy.spawns, progress.beat.call_count) == ([0.0], [1.0], 2)
    # And polls go on and beat until the cooldown ends, then try the abandoned connection again.
    await pacing.step(28)
    assert (hung.attempts, progress.beat.call_count) == ([0.0], 3)
    await pacing.step(1)
    assert (hung.attempts, progress.beat.call_count) == ([0.0, 30.0], 4)
    await stop_supervisor(task)


async def test_supervisor_spends_at_most_one_claim_budget_per_poll_however_many_claims_hang(monkeypatch):
    # Given three waiting connections whose claims all hang, and a small claim budget.
    monkeypatch.setattr(supervisor, "_CLAIM_TIMEOUT_SECONDS", 0.05)
    progress, logger = Mock(), Mock()
    monkeypatch.setattr(supervisor, "loop_progress", lambda: progress)
    monkeypatch.setattr(supervisor, "logger", logger)
    pacing = Pacing()
    children = [Scripted(pacing, hangs=True) for _ in range(3)]
    task, _ = supervise(monkeypatch, pacing, *children)
    # When the first poll runs.
    await pacing.paused()
    # Then it attempted exactly one claim, beat, and reported the abandonment with the two behind it deferred.
    assert [child.attempts for child in children] == [[0.0], [], []]
    assert progress.beat.call_count == 1
    assert abandoned_claims(logger) == [(children[0].connection.config_id, 2)]
    # When the next polls run.
    await pacing.step(1)
    await pacing.step(1)
    # Then each attempts the next connection, the timed-out one cooling down, and the last abandonment defers none.
    assert [child.attempts for child in children] == [[0.0], [1.0], [2.0]]
    assert progress.beat.call_count == 3
    assert abandoned_claims(logger) == [
        (child.connection.config_id, left) for child, left in zip(children, (2, 1, 0), strict=True)
    ]
    await stop_supervisor(task)


async def test_supervisor_still_continues_to_the_next_connection_after_a_claim_error_that_is_not_a_timeout(monkeypatch):
    # Given the oldest connection's claim raises an unclassified error and a healthy one waits behind it.
    pacing = Pacing()
    bad, healthy = Scripted(pacing, explodes, failing_stage="claim"), Scripted(pacing)
    task, _ = supervise(monkeypatch, pacing, bad, healthy)
    # When the first poll runs.
    await pacing.paused()
    # Then the same poll goes on to claim and start the healthy connection.
    assert (bad.attempts, healthy.spawns) == ([0.0], [0.0])
    await stop_supervisor(task)


async def test_supervisor_backs_off_when_candidates_fails(monkeypatch):
    # Given a candidates query that fails twice before recovering.
    monkeypatch.setattr(supervisor, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(supervisor, "_BACKOFF_START_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "_BACKOFF_MAX_SECONDS", 0.1)
    connection = pending_connection()
    calls: list[float] = []
    spawned = asyncio.Event()

    async def candidates():
        calls.append(monotonic())
        if len(calls) <= 2:
            raise RuntimeError("database unavailable")
        return [connection]

    async def claim(_config_id):
        return connection

    async def run_child(_connection, _command):
        spawned.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(supervisor, "candidates", candidates)
    monkeypatch.setattr(supervisor, "claim", claim)
    monkeypatch.setattr(supervisor, "run_child", run_child)
    # When the supervisor runs.
    task = asyncio.create_task(supervisor.personal_supervisor())
    await asyncio.wait_for(spawned.wait(), 5)
    # Then the failure never escapes, each retry waits out the growing backoff, and the connection is spawned.
    assert not task.done()
    assert calls[1] - calls[0] >= 0.05
    assert calls[2] - calls[1] >= 0.1
    await stop_supervisor(task)


def run_supervisor(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP) -> asyncio.Task[object]:
    monkeypatch.setattr(supervisor.anyio, "sleep", worker_http.pacing.wait)
    return worker_http.start_loop(
        "whatsapp_personal",
        supervisor.personal_supervisor(worker_http.pacing.clock),
        budget_seconds=supervisor.PROGRESS_BUDGET_SECONDS,
    )


async def test_supervisor_beats_after_every_completed_pass_including_an_idle_one(monkeypatch, worker_http):
    # Given a candidates read that always succeeds and finds nothing to supervise.
    monkeypatch.setattr(supervisor, "candidates", AsyncMock(return_value=[]))
    task = run_supervisor(monkeypatch, worker_http)
    await worker_http.pacing.paused()
    # When passes keep finishing for longer than the budget in total.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then every finished pass keeps the loop ready.
        await worker_http.ready(expected=True)
    assert not task.done()


async def test_supervisor_beats_every_pass_while_the_ceiling_holds_a_connection_back(monkeypatch, worker_http):
    # Given a ceiling of one running child and a second connection waiting behind it.
    fleet(monkeypatch, ceiling=1, per_poll=5)
    connections = [pending_connection(), pending_connection()]
    claimed = []

    async def claim(config_id):
        claimed.append(config_id)
        return next(connection for connection in connections if connection.config_id == config_id)

    async def run_child(_connection, _command):
        await forever()

    monkeypatch.setattr(supervisor, "candidates", AsyncMock(return_value=connections))
    monkeypatch.setattr(supervisor, "claim", claim)
    monkeypatch.setattr(supervisor, "run_child", run_child)
    task = run_supervisor(monkeypatch, worker_http)
    await worker_http.pacing.paused()
    # When passes keep finishing for longer than the budget in total.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then every capped pass still keeps the loop ready, and only the oldest connection ever started.
        await worker_http.ready(expected=True)
    assert claimed == [connections[0].config_id]
    assert not task.done()


async def test_supervisor_failed_candidates_read_goes_stale_even_with_a_live_bridge_child(monkeypatch, worker_http):
    # Given a running bridge child and a candidates read that starts failing.
    connection = pending_connection()
    candidates = AsyncMock(return_value=[connection])
    spawned, cancelled = asyncio.Event(), asyncio.Event()

    async def run_child(_connection, _command):
        spawned.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(supervisor, "candidates", candidates)
    monkeypatch.setattr(supervisor, "claim", AsyncMock(return_value=connection))
    monkeypatch.setattr(supervisor, "run_child", run_child)
    task = run_supervisor(monkeypatch, worker_http)
    await asyncio.wait_for(spawned.wait(), 2)
    await worker_http.pacing.paused()
    candidates.side_effect = OSError("database unavailable")
    # When failing passes outlast the budget.
    for _ in range(4):
        await worker_http.pacing.step()
    # Then readiness fails while health holds, and the loop and its child are neither cancelled nor restarted.
    await worker_http.ready(expected=False)
    assert not task.done()
    assert not cancelled.is_set()
    # And the first pass that completes restores readiness.
    candidates.side_effect = None
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)


@pytest.mark.parametrize("failing_stage", ["claim", "run_child"])
async def test_supervisor_bridge_child_failures_never_make_the_loop_stale(monkeypatch, worker_http, failing_stage):
    # Given a connection whose child task fails every time it is respawned.
    connection = pending_connection()
    claims: list[object] = []
    respawned = asyncio.Event()

    async def claim(config_id):
        claims.append(config_id)
        if len(claims) == 2:
            respawned.set()
        if failing_stage == "claim":
            raise RuntimeError("claim exploded")
        return connection

    async def run_child(_connection, _command):
        raise RuntimeError("child exploded")

    monkeypatch.setattr(supervisor, "candidates", AsyncMock(return_value=[connection]))
    monkeypatch.setattr(supervisor, "claim", claim)
    monkeypatch.setattr(supervisor, "run_child", run_child)
    task = run_supervisor(monkeypatch, worker_http)
    await worker_http.pacing.paused()
    # When passes keep finishing for longer than the budget in total.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then the failing child never makes the loop stale.
        await worker_http.ready(expected=True)
    await asyncio.wait_for(respawned.wait(), 2)
    assert not task.done()


async def test_supervisor_cooling_down_children_never_keep_a_failing_candidates_read_ready(monkeypatch, worker_http):
    # Given a connection whose child failed and is cooling down, and a candidates read that then starts failing.
    connection = pending_connection()
    candidates = AsyncMock(return_value=[connection])
    spawned = asyncio.Event()

    async def claim(_config_id):
        spawned.set()
        raise RuntimeError("claim exploded")

    monkeypatch.setattr(supervisor, "candidates", candidates)
    monkeypatch.setattr(supervisor, "claim", claim)
    task = run_supervisor(monkeypatch, worker_http)
    await asyncio.wait_for(spawned.wait(), 2)
    await worker_http.pacing.paused()
    candidates.side_effect = OSError("database unavailable")
    # When failing passes outlast the budget.
    for _ in range(4):
        await worker_http.pacing.step()
    # Then neither the failed child nor its cooldown keeps the loop ready.
    await worker_http.ready(expected=False)
    assert not task.done()
