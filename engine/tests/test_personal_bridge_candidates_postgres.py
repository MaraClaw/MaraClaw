import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import anyio
import pytest
from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db
from tests.relationship_postgres_fixtures import statements

from app.config import get_settings
from app.dao import (
    personal_bridge_fleet,
    whatsapp_personal_dao,
    whatsapp_personal_runtime_dao,
    zalo_personal_dao,
    zalo_personal_runtime_dao,
)
from app.dao.personal_bridge_fleet import FleetFullError
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.services.channels import whatsapp_personal_control, zalo_personal_control

__all__ = ["channel_db", "receipt_db", "statements"]

_NOW = datetime.now(UTC)


class Listed(Protocol):
    @property
    def config_id(self) -> UUID: ...


class Gated(Protocol):
    @property
    def retry_at(self) -> datetime | None: ...


@dataclass(frozen=True, slots=True)
class Bridge:
    insert: str
    eligible: str
    channel: personal_bridge_fleet.Bridge
    runtime_key: str
    state_gated: bool
    candidates: Callable[[], Awaitable[Sequence[Listed]]]
    claim: Callable[[UUID], Awaitable[object | None]]
    runtime_for: Callable[[UUID], Awaitable[Gated]]
    start: Callable[[UUID, UUID], Awaitable[object]]
    lock_row: str
    expire_lease: str


async def _start_whatsapp(agent_id: UUID, actor_id: UUID) -> object:
    return await whatsapp_personal_control.start(agent_id, actor_id, None)


async def _start_zalo(agent_id: UUID, actor_id: UUID) -> object:
    return await zalo_personal_control.start(agent_id, actor_id, None)


BRIDGES = [
    pytest.param(
        Bridge(
            "INSERT INTO whatsapp_personal_connections (config_id, agent_id, tenant_id, owner_id, auth_epoch, "
            "generation, attempt_id, state, desired, claim_token, lease_until, created_at, updated_at) "
            "VALUES (%(id)s, %(agent)s, %(tenant)s, %(owner)s, %(epoch)s, %(generation)s, %(attempt)s, "
            "%(state)s, %(desired)s, %(claim)s, %(lease)s, %(created)s, %(updated)s)",
            "SELECT config_id FROM whatsapp_personal_connections WHERE desired AND claim_token IS NULL",
            "whatsapp",
            "whatsapp_runtime",
            False,
            whatsapp_personal_dao.candidates,
            whatsapp_personal_dao.claim,
            whatsapp_personal_runtime_dao.runtime_for,
            _start_whatsapp,
            "SELECT TRUE FROM whatsapp_personal_connections WHERE config_id=%(id)s FOR UPDATE",
            "UPDATE whatsapp_personal_connections SET lease_until=now()-interval '1 second' WHERE config_id=%(id)s",
        ),
        id="whatsapp",
    ),
    pytest.param(
        Bridge(
            "INSERT INTO zalo_personal_connections (config_id, agent_id, tenant_id, owner_id, auth_epoch, "
            "generation, attempt_id, state, desired, claim_token, lease_until, created_at, updated_at) "
            "VALUES (%(id)s, %(agent)s, %(tenant)s, %(owner)s, %(epoch)s, %(generation)s, %(attempt)s, "
            "%(state)s, %(desired)s, %(claim)s, %(lease)s, %(created)s, %(updated)s)",
            "SELECT config_id FROM zalo_personal_connections "
            "WHERE desired AND claim_token IS NULL AND state IN ('pending','reconnecting')",
            "zalo",
            "zalo_runtime",
            True,
            zalo_personal_dao.candidates,
            zalo_personal_dao.claim,
            zalo_personal_runtime_dao.runtime_for,
            _start_zalo,
            "SELECT TRUE FROM zalo_personal_connections WHERE config_id=%(id)s FOR UPDATE",
            "UPDATE zalo_personal_connections SET lease_until=now()-interval '1 second' WHERE config_id=%(id)s",
        ),
        id="zalo",
    ),
]

_MISSING = object()


async def seed(
    f: ChannelDatabase,
    bridge: Bridge,
    *,
    runtime: object = _MISSING,
    updated_at: datetime = _NOW,
    created_at: datetime = _NOW,
    desired: bool = True,
    claimed: bool = False,
    lease_in: timedelta = timedelta(seconds=30),
    state: str = "reconnecting",
) -> UUID:
    """One connection row. ``runtime`` is the value under the bridge's runtime key; ``_MISSING`` omits the config row."""
    agent_id, config_id = uuid4(), uuid4()
    db = f.db.first
    if runtime is not _MISSING:
        await db.execute(
            "INSERT INTO agents (id, name, creator_id, tenant_id, role_description, max_triggers, "
            "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
            "VALUES (%(id)s, 'Fleet QA', %(user)s, %(tenant)s, '', 20, 5, 5, 'private', 'use', 240)",
            {"id": agent_id, "user": f.db.user.id, "tenant": f.tenant_id},
        )
        await db.execute(
            "INSERT INTO channel_configs (id, agent_id, channel_type, is_configured, is_connected, extra_config) "
            "VALUES (%(id)s, %(agent)s, %(channel)s, TRUE, FALSE, %(extra)s::json)",
            {
                "id": config_id,
                "agent": agent_id,
                "channel": bridge.channel,
                "extra": json.dumps(runtime_extra(bridge, runtime)),
            },
        )
    await db.execute(
        bridge.insert,
        {
            "id": config_id,
            "agent": agent_id,
            "tenant": f.tenant_id,
            "owner": f.db.user.id,
            "epoch": uuid4(),
            "generation": uuid4(),
            "attempt": uuid4(),
            "state": state,
            "desired": desired,
            "claim": uuid4() if claimed else None,
            "lease": _NOW + lease_in if claimed else None,
            "created": created_at,
            "updated": updated_at,
        },
    )
    return config_id


def runtime_extra(bridge: Bridge, runtime: object) -> object:
    """``None`` stands for an extra_config that is not an object at all; otherwise the runtime sits under its key."""
    return [] if runtime is None else {bridge.runtime_key: runtime}


def gate(offset: timedelta) -> dict[str, object]:
    return {"reconnect_attempts": 2, "retry_at": (_NOW + offset).isoformat()}


async def oracle(f: ChannelDatabase, bridge: Bridge) -> set[UUID]:
    """The due set the per-row implementation produced: every eligible row whose own runtime read passes the gate."""
    rows = await f.db.first.fetchall(bridge.eligible)
    due: set[UUID] = set()
    for row in rows:
        config_id = row["config_id"]
        assert isinstance(config_id, UUID)
        runtime = await bridge.runtime_for(config_id)
        if runtime.retry_at is None or runtime.retry_at <= datetime.now(UTC):
            due.add(config_id)
    return due


@pytest.mark.parametrize("bridge", BRIDGES)
async def test_candidates_return_the_due_set_the_per_row_runtime_reads_produced(channel_db, bridge):
    # Given connections covering every retry-gate shape, a missing config row, and rows the query must exclude.
    f = channel_db
    expected: set[UUID] = set()
    async with transaction(f.db.first):
        for runtime in (
            {},
            {"reconnect_attempts": 4, "retry_at": None},
            gate(timedelta(hours=-1)),
            {"reconnect_attempts": "many", "retry_at": (_NOW + timedelta(hours=1)).isoformat()},
            {"retry_at": "tomorrow-ish"},
            5,
            None,
            _MISSING,
        ):
            expected.add(await seed(f, bridge, runtime=runtime))
        await seed(f, bridge, runtime=gate(timedelta(hours=1)))
        await seed(f, bridge, runtime=gate(timedelta(days=30)))
        await seed(f, bridge, runtime={}, claimed=True)
        await seed(f, bridge, runtime={}, desired=False)
        paused = await seed(f, bridge, runtime={}, state="paused")
        if not bridge.state_gated:
            expected.add(paused)
        # When the candidates are read.
        listed = [candidate.config_id for candidate in await bridge.candidates()]
        # Then they are exactly the rows the per-row implementation selected, each listed once.
        assert set(listed) == expected == await oracle(f, bridge)
        assert len(listed) == len(expected)


@pytest.mark.parametrize("bridge", BRIDGES)
async def test_candidates_list_the_longest_waiting_connection_first(channel_db, bridge):
    # Given connections released or gated at known instants; ``created_at`` is deliberately in a different order.
    f = channel_db
    async with transaction(f.db.first):
        newest_release = await seed(
            f, bridge, runtime={}, updated_at=_NOW - timedelta(seconds=50), created_at=_NOW - timedelta(days=9)
        )
        oldest_release = await seed(
            f, bridge, runtime={}, updated_at=_NOW - timedelta(seconds=300), created_at=_NOW - timedelta(days=1)
        )
        gate_opened_late = await seed(
            f,
            bridge,
            runtime=gate(timedelta(seconds=-200)),
            updated_at=_NOW - timedelta(seconds=500),
            created_at=_NOW - timedelta(days=2),
        )
        middle_release = await seed(
            f, bridge, runtime={}, updated_at=_NOW - timedelta(seconds=100), created_at=_NOW - timedelta(days=3)
        )
        # When the candidates are read.
        listed = [candidate.config_id for candidate in await bridge.candidates()]
    # Then they are ordered by when each became startable, a retry gate counting from the moment it opened.
    assert listed == [oldest_release, gate_opened_late, middle_release, newest_release]


@pytest.mark.parametrize("bridge", BRIDGES)
async def test_candidate_reads_cost_the_same_statements_however_many_connections_wait(channel_db, statements, bridge):
    # Given a few connections waiting, then many more with and without retry gates.
    f = channel_db
    counts: list[int] = []
    async with transaction(f.db.first):
        for total in (2, 14):
            while len(await bridge.candidates()) < total - 1:
                await seed(f, bridge, runtime=gate(timedelta(hours=-1)))
            await seed(f, bridge, runtime=gate(timedelta(hours=1)))
            statements.clear()
            # When the candidates are read.
            listed = await bridge.candidates()
            counts.append(len(statements))
            assert len(listed) == total - 1
    # Then the statement count does not grow with the fleet.
    assert counts == [1, 1]


@pytest.mark.parametrize("bridge", BRIDGES)
async def test_claim_rechecks_the_retry_gate_after_a_connection_was_listed_as_due(channel_db, bridge):
    # Given a started connection that candidates() lists as due.
    f = channel_db
    async with transaction(f.db.first):
        await bridge.start(f.db.agent.id, f.db.user.id)
        (listed,) = await bridge.candidates()

        async def set_gate(at: datetime) -> None:
            await f.db.first.execute(
                "UPDATE channel_configs SET extra_config=jsonb_build_object(%(key)s::text,"
                "jsonb_build_object('reconnect_attempts',1,'retry_at',to_jsonb(%(at)s::timestamptz)))::json "
                "WHERE agent_id=%(agent)s AND channel_type=%(channel)s",
                {"key": bridge.runtime_key, "at": at, "agent": f.db.agent.id, "channel": bridge.channel},
            )

        # When an interruption commits a retry gate before the claim.
        await set_gate(datetime.now(UTC) + timedelta(hours=1))
        # Then the claim still refuses it under its locks, and takes it once the gate has opened.
        assert await bridge.claim(listed.config_id) is None
        await set_gate(datetime.now(UTC) - timedelta(seconds=1))
        assert await bridge.claim(listed.config_id) is not None


def fleet_ceiling(monkeypatch: pytest.MonkeyPatch, bridge: Bridge, ceiling: int) -> None:
    monkeypatch.setattr(get_settings(), f"{bridge.channel.upper()}_PERSONAL_MAX_RUNNING_CHILDREN", ceiling)


async def unclaimed(bridge: Bridge) -> set[UUID]:
    return {candidate.config_id for candidate in await bridge.candidates()}


@pytest.mark.parametrize("bridge", BRIDGES)
async def test_claim_is_refused_at_the_fleet_ceiling_and_expired_leases_do_not_count(channel_db, monkeypatch, bridge):
    # Given a ceiling of two held by one live lease and one expired lease, and two waiting connections.
    f = channel_db
    fleet_ceiling(monkeypatch, bridge, 2)
    async with transaction(f.db.first):
        live = await seed(f, bridge, claimed=True, lease_in=timedelta(hours=1))
        await seed(f, bridge, claimed=True, lease_in=timedelta(hours=-1))
        first, second = await seed(f, bridge, runtime={}), await seed(f, bridge, runtime={})
        # When the waiting connections claim in turn.
        assert await bridge.claim(first) is not None
        with pytest.raises(FleetFullError) as refused:
            await bridge.claim(second)
        # Then the expired lease never counted, and the refusal reports the fleet-wide count and wrote nothing.
        assert (refused.value.live, refused.value.ceiling) == (2, 2)
        assert await unclaimed(bridge) == {second}
        # And once the live lease expires the refused connection is claimed.
        await f.db.first.execute(bridge.expire_lease, {"id": live})
        assert await bridge.claim(second) is not None


@pytest.mark.parametrize("bridge", BRIDGES)
async def test_a_claim_queues_behind_the_fleet_lock_and_counts_the_lease_it_waited_for(channel_db, monkeypatch, bridge):
    # Given a ceiling of one, two waiting connections, and a first claim that has leased its row but not committed.
    f = channel_db
    fleet_ceiling(monkeypatch, bridge, 1)
    async with transaction(f.db.first):
        first, second = await seed(f, bridge, runtime={}), await seed(f, bridge, runtime={})
    leased, release = anyio.Event(), anyio.Event()
    outcomes: dict[str, object] = {}

    async def hold() -> None:
        async with transaction(f.db.first):
            outcomes["first"] = await bridge.claim(first)
            leased.set()
            await release.wait()

    async def compete() -> None:
        await leased.wait()
        async with transaction(f.db.second):
            try:
                outcomes["second"] = await bridge.claim(second)
            except FleetFullError as refused:
                outcomes["second"] = refused

    # When a second claimer on another connection starts while the first is uncommitted.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(hold)
            tasks.start_soon(compete)
            await leased.wait()
            while "second" not in outcomes and not await f.db.first.fetchval(
                "SELECT cardinality(pg_blocking_pids(%(pid)s))", {"pid": f.db.second.raw.info.backend_pid}
            ):
                pass
            release.set()
    # Then it queued behind the first, counted the lease that committed and was refused: one live lease, never two.
    refused = outcomes["second"]
    assert outcomes["first"] is not None
    assert isinstance(refused, FleetFullError)
    assert (refused.live, refused.ceiling) == (1, 1)
    async with transaction(f.db.first):
        assert await unclaimed(bridge) == {second}


@pytest.mark.parametrize("bridge", BRIDGES)
async def test_claimers_on_separate_connections_win_exactly_the_ceiling(channel_db, monkeypatch, bridge):
    # Given a ceiling of three and eight waiting connections shared between two claimers on separate connections.
    f = channel_db
    fleet_ceiling(monkeypatch, bridge, 3)
    async with transaction(f.db.first):
        waiting = [await seed(f, bridge, runtime={}) for _ in range(8)]
    won: list[UUID] = []
    refused: list[UUID] = []

    async def claimer(db, config_ids: list[UUID]) -> None:
        for config_id in config_ids:
            async with transaction(db):
                try:
                    if await bridge.claim(config_id) is not None:
                        won.append(config_id)
                except FleetFullError:
                    refused.append(config_id)

    # When both claim every connection in their share concurrently.
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(claimer, f.db.first, waiting[::2])
            tasks.start_soon(claimer, f.db.second, waiting[1::2])
    # Then exactly the ceiling won and the rest were refused, leaving only the refused connections unclaimed.
    assert (len(won), len(refused)) == (3, 5)
    async with transaction(f.db.first):
        assert await unclaimed(bridge) == set(refused)


@pytest.mark.parametrize("bridge", BRIDGES)
async def test_a_connection_row_locked_elsewhere_is_declined_without_waiting(channel_db, bridge):
    # Given a waiting connection whose row another transaction holds locked.
    f = channel_db
    async with transaction(f.db.first):
        config_id = await seed(f, bridge, runtime={})
    async with transaction(f.db.second):
        await f.db.second.fetchval(bridge.lock_row, {"id": config_id})
        # When a claim runs meanwhile.
        with anyio.fail_after(5):
            async with transaction(f.db.first):
                declined = await bridge.claim(config_id)
        # Then it is declined at once, as a decline rather than a fleet refusal, and wrote nothing.
        assert declined is None
        assert await unclaimed(bridge) == {config_id}
    # And it is claimed once the row is free.
    async with transaction(f.db.first):
        assert await bridge.claim(config_id) is not None


@asynccontextmanager
async def real_pool(f: ChannelDatabase) -> AsyncGenerator[None]:
    await init_pool(conninfo=f.db.conninfo, min_size=1, max_size=1, pool_timeout=5)
    try:
        yield
    finally:
        await close_pool()


@pytest.mark.parametrize("bridge", BRIDGES)
async def test_a_claim_cancelled_while_queued_for_the_fleet_lock_rolls_back_and_returns_its_connection(
    channel_db, bridge
):
    # Given a waiting connection, a one-connection pool, and another session holding the bridge's fleet lock.
    f = channel_db
    async with transaction(f.db.first):
        config_id = await seed(f, bridge, runtime={})
    locked, release = anyio.Event(), anyio.Event()

    async def hold_fleet_lock() -> None:
        async with transaction(f.db.second):
            await f.db.second.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 0))",
                {"key": personal_bridge_fleet.LOCK_KEYS[bridge.channel]},
            )
            locked.set()
            await release.wait()

    async def claim_is_queued() -> bool:
        async with transaction(f.db.first):
            return bool(
                await f.db.first.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                    "AND wait_event_type='Lock' AND wait_event='advisory'"
                )
            )

    async with real_pool(f), anyio.create_task_group() as holder:
        holder.start_soon(hold_fleet_lock)
        await locked.wait()
        # When a claim queues behind that lock and is then cancelled.
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(bridge.claim, config_id)
                while not await claim_is_queued():
                    pass
                tasks.cancel_scope.cancel()
        # Then nothing was leased, and the pool's only connection is back: a one-connection pool serves this read.
        assert await unclaimed(bridge) == {config_id}
        release.set()
    # And once the lock is free the next claim gets that connection and wins.
    async with real_pool(f):
        assert await bridge.claim(config_id) is not None
