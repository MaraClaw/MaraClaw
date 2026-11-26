import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db
from tests.relationship_postgres_fixtures import statements

from app.dao import whatsapp_personal_dao, whatsapp_personal_runtime_dao, zalo_personal_dao, zalo_personal_runtime_dao
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
    channel: str
    runtime_key: str
    state_gated: bool
    candidates: Callable[[], Awaitable[Sequence[Listed]]]
    claim: Callable[[UUID], Awaitable[object | None]]
    runtime_for: Callable[[UUID], Awaitable[Gated]]
    start: Callable[[UUID, UUID], Awaitable[object]]


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
    state: str = "reconnecting",
) -> UUID:
    """One connection row. ``runtime`` is the value under the bridge's runtime key; ``_MISSING`` omits the config row."""
    agent_id, config_id = uuid4(), uuid4()
    db = f.db.first
    if runtime is not _MISSING:
        await db.execute(
            "INSERT INTO agents (id, name, creator_id, role_description, max_triggers, min_poll_interval_min, "
            "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
            "VALUES (%(id)s, 'Fleet QA', %(user)s, '', 20, 5, 5, 'private', 'use', 240)",
            {"id": agent_id, "user": f.db.user.id},
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
            "lease": _NOW + timedelta(seconds=30) if claimed else None,
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
