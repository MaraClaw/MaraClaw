import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.zalo_personal_fixtures import bind, channel_db, connected, receipt_db, route_for

from app.config import get_settings
from app.dao import zalo_personal_dao
from app.dao.zalo_personal_auth_dao import read_auth
from app.dao.zalo_personal_dao import candidates, claim, get_personal, reaped, renew, revoke
from app.dao.zalo_personal_runtime_dao import interrupted, runtime_for
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk
from app.records.zalo_personal import ZaloConnection
from app.services.channels import zalo_personal_supervisor as supervisor
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError
from app.services.channels.zalo_personal_control import start, status
from app.services.channels.zalo_personal_events import handle_frame
from app.services.channels.zalo_personal_journal import authorize_dispatch, enqueue
from app.services.channels.zalo_personal_protocol import Closed, Heartbeat, Ready
from worker_progress_fixtures import Pacing

__all__ = ["channel_db", "receipt_db"]


async def test_transport_close_preserves_encrypted_login_and_routing_fences(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        with pytest.raises(PersonalSendPendingError):
            await enqueue(route_for(owned).external_id(), OutboundChunk(text="pending", key=uuid4()))
        dispatched = await authorize_dispatch(owned)
        assert dispatched is not None
        with pytest.raises(PersonalSendPendingError):
            await enqueue(route_for(owned).external_id(), OutboundChunk(text="still pending", key=uuid4()))
        sealed = await f.db.first.fetchval("SELECT ciphertext FROM zalo_personal_auth")
        await handle_frame(owned, Closed(type="closed", id="drop", token="test", reason="transport_failed"))
        current = await get_personal(owned.agent_id)
        assert current is not None
        assert current.desired
        assert current.state == "reconnecting"
        assert current.claim_token == owned.claim_token
        assert current.auth_epoch == owned.auth_epoch
        assert current.generation == owned.generation
        assert await f.db.first.fetchval("SELECT ciphertext FROM zalo_personal_auth") == sealed
        assert await reaped(owned)
        current = await get_personal(owned.agent_id)
        assert current is not None
        assert current.claim_token is None
        assert current.account_id == owned.account_id
        assert (
            await f.db.first.fetchval(
                "SELECT state FROM zalo_personal_sends WHERE command_id=%(id)s", {"id": dispatched.command_id}
            )
            == "uncertain"
        )
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_sends WHERE state='pending'") == 1
        assert await claim(owned.config_id) is None
        assert await candidates() == []


async def test_backoff_is_durable_and_claim_race_has_one_winner(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        assert await interrupted(owned)
        assert not await interrupted(owned)
        assert (await runtime_for(owned.config_id)).reconnect_attempts == 1
        await reaped(owned)
        assert await candidates() == []
        assert await claim(owned.config_id) is None
        await f.db.first.execute(
            "UPDATE channel_configs SET extra_config=(extra_config::jsonb || "
            "jsonb_build_object('zalo_runtime',extra_config::jsonb->'zalo_runtime' || "
            "jsonb_build_object('retry_at',now()-interval '1 second')))::json"
        )
        assert len(await candidates()) == 1
    winners = []

    async def acquire(db):
        async with transaction(db):
            winners.append(await claim(owned.config_id))

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(acquire, f.db.first)
            tasks.start_soon(acquire, f.db.second)
    winner = next(row for row in winners if row is not None)
    assert sum(row is not None for row in winners) == 1
    assert winner.state == "reconnecting"
    assert winner.expires_at is not None
    assert 110 < (winner.expires_at - datetime.now(UTC)).total_seconds() <= 120


async def test_retry_exhaustion_resume_and_ready_flaps_preserve_budget(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        sealed = await f.db.first.fetchval("SELECT ciphertext FROM zalo_personal_auth")
        current = owned
        for attempt, delay in enumerate((5, 15, 30, 60, 120), start=1):
            before = datetime.now(UTC)
            assert await interrupted(current)
            runtime = await runtime_for(owned.config_id)
            assert runtime.reconnect_attempts == attempt
            assert runtime.retry_at is not None
            assert delay <= (runtime.retry_at - before).total_seconds() < delay + 2
            assert not await interrupted(current)
            await reaped(current)
            await f.db.first.execute(
                "UPDATE channel_configs SET extra_config=jsonb_set(extra_config::jsonb,"
                "'{zalo_runtime,retry_at}',to_jsonb(now()-interval '1 second'))::json"
            )
            restored = await claim(owned.config_id)
            assert restored is not None
            current = restored
            await handle_frame(current, Ready(type="ready", id="ready", token="test"))
            assert (await runtime_for(owned.config_id)).reconnect_attempts == attempt
        assert await interrupted(current)
        await reaped(current)
        paused = await status(owned.agent_id, f.db.user.id)
        assert paused.state == "failed"
        assert paused.resumable
        assert paused.diagnostics is not None
        assert paused.diagnostics.reconnect_attempts == 5
        assert await candidates() == []
        resumed = await start(owned.agent_id, f.db.user.id, paused.generation)
        assert resumed.generation == owned.generation
        assert resumed.state == "reconnecting"
        assert resumed.qr is None
        assert resumed.diagnostics is not None
        assert resumed.diagnostics.reconnect_attempts == 0
        current = await claim(owned.config_id)
        assert current is not None
        assert current.auth_epoch == owned.auth_epoch
        assert await read_auth(current) == "opaque-session"
        assert await f.db.first.fetchval("SELECT ciphertext FROM zalo_personal_auth") == sealed
        await handle_frame(current, Ready(type="ready", id="ready", token="test"))
        await handle_frame(current, Heartbeat(type="heartbeat", id="ping", token="test", ok=True))
        await interrupted(current)
        assert (await runtime_for(owned.config_id)).reconnect_attempts == 1


async def test_session_replacement_pauses_and_relink_explicitly_purges(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await handle_frame(owned, Closed(type="closed", id="closed", token="test", reason="session_replaced"))
        await reaped(owned)
        paused = await status(owned.agent_id, f.db.user.id)
        assert paused.state == "paused"
        assert paused.resumable
        assert paused.diagnostics is not None
        assert paused.diagnostics.last_error == "session_replaced"
        assert paused.diagnostics.retry_at is None
        assert await candidates() == []
        await start(owned.agent_id, f.db.user.id, paused.generation, relink=True)
        row = await get_personal(owned.agent_id)
        assert row is not None
        assert row.auth_epoch != owned.auth_epoch
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_auth") == 0


@pytest.mark.parametrize("change", ["revoke", "owner", "tenant"])
async def test_stale_sources_cannot_renew_or_resurrect_retry(channel_db, change):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        if change == "revoke":
            await revoke(owned.agent_id, purge=False)
        elif change == "owner":
            await f.db.first.execute("UPDATE agents SET creator_id=%(id)s", {"id": f.sender_id})
        else:
            await f.db.first.execute("UPDATE agents SET tenant_id=NULL")
        current = await get_personal(owned.agent_id)
        assert current is not None
        with pytest.raises(HTTPException):
            await renew(owned)
        assert not await interrupted(owned)
        assert await get_personal(owned.agent_id) == current


async def test_restore_expiry_recovers_and_status_does_not_expire_backoff(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await reaped(owned)
        current = await claim(owned.config_id)
        assert current is not None
        await f.db.first.execute("UPDATE zalo_personal_connections SET expires_at=now()-interval '1 second'")
        public = await status(owned.agent_id, f.db.user.id)
        assert public.state == "reconnecting"
        assert public.error_code == "transport_failed"
        await reaped(current)
        await f.db.first.execute("UPDATE zalo_personal_connections SET expires_at=now()-interval '1 second'")
        assert (await status(owned.agent_id, f.db.user.id)).state == "reconnecting"
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_auth") == 1


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

    def __init__(self, pacing: Pacing, *script: Callable[[], Awaitable[None]], declines: bool = False) -> None:
        self.connection = ZaloConnection(
            config_id=uuid4(),
            agent_id=uuid4(),
            tenant_id=uuid4(),
            owner_id=uuid4(),
            auth_epoch=uuid4(),
            generation=uuid4(),
            attempt_id=uuid4(),
            revision=1,
            state="connected",
            desired=True,
            created_at=datetime.now(UTC),
        )
        self.pacing = pacing
        self.script = list(script)
        self.declines = declines
        self.attempts: list[float] = []
        self.spawns: list[float] = []

    async def claim(self) -> ZaloConnection | None:
        self.attempts.append(self.pacing.now)
        if self.declines:
            return None
        self.spawns.append(self.pacing.now)
        return self.connection

    async def run_child(self) -> None:
        await (self.script.pop(0) if self.script else forever)()


def supervise(
    monkeypatch: pytest.MonkeyPatch, pacing: Pacing, *children: Scripted, value: float = 0.0
) -> tuple[asyncio.Task[None], AsyncMock, list[float]]:
    by_id = {child.connection.config_id: child for child in children}
    found = AsyncMock(return_value=[child.connection for child in children])
    sleeps: list[float] = []

    async def claim_one(config_id):
        return await by_id[config_id].claim()

    async def run_child(connection, _command):
        await by_id[connection.config_id].run_child()

    async def sleep(seconds):
        sleeps.append(seconds)
        await pacing.wait(seconds)

    monkeypatch.setattr(supervisor, "candidates", found)
    monkeypatch.setattr(supervisor, "claim", claim_one)
    monkeypatch.setattr(supervisor, "run_child", run_child)
    monkeypatch.setattr(supervisor.anyio, "sleep", sleep)
    return asyncio.create_task(supervisor.personal_supervisor(pacing.clock, lambda: value)), found, sleeps


async def stop_supervisor(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.parametrize(("value", "cooldowns"), [(0.0, [30, 60, 120, 150, 150]), (1.0, [60, 120, 240, 300, 300])])
async def test_supervisor_leaves_a_failed_child_at_once_and_escalates_its_cooldown(monkeypatch, value, cooldowns):
    # Given one connection whose child raises an unclassified error every time, and a healthy sibling.
    pacing, fail = Pacing(), AsyncMock()
    monkeypatch.setattr(zalo_personal_dao, "fail", fail)
    bad, good = Scripted(pacing, *[explodes] * 8), Scripted(pacing)
    task, _, sleeps = supervise(monkeypatch, pacing, bad, good, value=value)
    await pacing.paused()
    assert bad.spawns == [0.0]
    # When the clock runs through each cooldown.
    for respawns, cooldown in enumerate(cooldowns, start=1):
        await pacing.step(cooldown - 1)
        # Then the connection stays down until its cooldown ends, and the next cooldown is longer.
        assert len(bad.spawns) == respawns
        await pacing.step(1)
        assert len(bad.spawns) == respawns + 1
    # And the only waits are the one-second polls: no child sleeps its cooldown while still running.
    assert not task.done()
    assert len(good.spawns) == 1
    assert set(sleeps) == {1}
    fail.assert_not_awaited()
    await stop_supervisor(task)


@pytest.mark.parametrize(
    ("run_seconds", "cooldown"),
    [(supervisor._HEALTHY_CHILD_SECONDS - 1, 60), (supervisor._HEALTHY_CHILD_SECONDS, 30)],
)
async def test_supervisor_restarts_escalation_only_after_a_healthy_child_run(monkeypatch, run_seconds, cooldown):
    # Given a connection that fails fast once and then fails again after running for run_seconds.
    pacing, release = Pacing(), asyncio.Event()
    bad = Scripted(pacing, explodes, lives_until(release, then_explodes=True))
    task, _, _ = supervise(monkeypatch, pacing, bad)
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
    task, _, _ = supervise(monkeypatch, pacing, bad)
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
    task, found, _ = supervise(monkeypatch, pacing, bad)
    await pacing.paused()
    found.return_value = []
    await pacing.step(1)
    # When it comes back as a candidate.
    found.return_value = [bad.connection]
    await pacing.step(1)
    # Then it is not held back by its old cooldown, and its escalation has started over.
    assert bad.spawns == [0.0, 2.0]
    await pacing.step(29)
    assert len(bad.spawns) == 2
    await pacing.step(1)
    assert bad.spawns == [0.0, 2.0, 32.0]
    await stop_supervisor(task)


def fleet(monkeypatch: pytest.MonkeyPatch, *, ceiling: int, per_poll: int) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ZALO_PERSONAL_MAX_RUNNING_CHILDREN", ceiling)
    monkeypatch.setattr(settings, "ZALO_PERSONAL_MAX_STARTS_PER_POLL", per_poll)


def started(children: Sequence[Scripted]) -> list[int]:
    return [index for index, child in enumerate(children) if child.spawns]


async def test_supervisor_brings_a_fleet_back_in_paced_waves_under_the_ceiling(monkeypatch):
    # Given seven waiting connections, oldest first, a ceiling of five running children and two starts per poll.
    fleet(monkeypatch, ceiling=5, per_poll=2)
    pacing, first_exit, logger = Pacing(), asyncio.Event(), Mock()
    monkeypatch.setattr(supervisor, "logger", logger)
    children = [Scripted(pacing, lives_until(first_exit, then_explodes=False))]
    children += [Scripted(pacing) for _ in range(6)]
    task, found, _ = supervise(monkeypatch, pacing, *children)
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
    found.return_value = [child.connection for child in children[1:]]
    await pacing.step(1)
    # Then exactly one waiting connection, the oldest, takes the free slot.
    assert started(children) == [0, 1, 2, 3, 4, 5]
    await pacing.step(1)
    assert children[6].spawns == []
    await stop_supervisor(task)


async def test_supervisor_skips_a_cooling_down_connection_without_spending_the_start_pace(monkeypatch):
    # Given the oldest connection fails at once and two healthy ones wait behind it, with one start per poll.
    fleet(monkeypatch, ceiling=10, per_poll=1)
    pacing = Pacing()
    bad, first, second = Scripted(pacing, explodes), Scripted(pacing), Scripted(pacing)
    task, _, _ = supervise(monkeypatch, pacing, bad, first, second)
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
    await stop_supervisor(task)


async def test_supervisor_does_not_spend_the_start_pace_on_a_declined_claim(monkeypatch):
    # Given two oldest connections whose claims are declined, three healthy ones behind them and two starts per poll.
    fleet(monkeypatch, ceiling=10, per_poll=2)
    pacing = Pacing()
    declined = [Scripted(pacing, declines=True) for _ in range(2)]
    healthy = [Scripted(pacing) for _ in range(3)]
    task, _, _ = supervise(monkeypatch, pacing, *declined, *healthy)
    await pacing.paused()
    # When polls pass.
    await pacing.step(1)
    # Then the declined claims are tried again every poll and the healthy connections still start two per poll.
    assert [child.attempts for child in declined] == [[0.0, 1.0], [0.0, 1.0]]
    assert [child.spawns for child in healthy] == [[0.0], [0.0], [1.0]]
    await stop_supervisor(task)


async def test_supervisor_cancellation_ends_at_once_while_a_cooldown_is_pending(monkeypatch):
    # Given a failed connection that is cooling down.
    pacing = Pacing()
    bad = Scripted(pacing, explodes)
    task, _, _ = supervise(monkeypatch, pacing, bad)
    await pacing.paused()
    # When the supervisor is cancelled.
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=2)
    # Then it ends at once, cancelled, without waiting out the cooldown.
    assert task in done
    assert task.cancelled()
    assert bad.spawns == [0.0]
