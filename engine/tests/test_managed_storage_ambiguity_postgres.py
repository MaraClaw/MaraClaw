"""Ambiguous provider outcomes, cancellation, restart and ledger outages (opt-in ``MARACLAW_TEST_POSTGRES_DSN``)."""

import hashlib
from contextlib import AsyncExitStack

import anyio
import pytest
from botocore.exceptions import ClientError

from agent_deletion_fixtures import deletion_scenario
from app.db.errors import DbError
from app.db.pool import close_pool, get_pool, init_pool
from app.db.session import connection_ctx
from app.records.organization_plan import OrganizationPlanId
from app.services.storage_runtime import quota_ledger
from app.services.storage_runtime.quota_errors import (
    StorageAccountingUnavailableError,
    StorageKeyPendingError,
)
from app.services.storage_runtime.quota_keys import ManagedKey
from app.services.storage_runtime.quota_ledger import Reservation, reserve_write
from managed_storage_fixtures import StorageWorld, quota_world
from s3_wire_server import Gate
from tier_limit_fixtures import approve_admission_for

__all__ = ["deletion_scenario", "quota_world"]

LIMIT, CEILING = 1000, 400


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


async def test_a_write_that_landed_but_lost_its_response_stays_charged_and_blocks_even_a_matching_head(
    quota_world: StorageWorld,
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    world.s3.state.put_faults[key] = "apply_then_drop"
    with pytest.raises(StorageKeyPendingError):
        await world.backend.write_bytes(key, b"a" * 300)
    pending = await world.row(agent)
    assert pending is not None
    assert (pending.pending, pending.outcome_unknown, pending.charged_bytes, await world.usage(agent)) == (
        True,
        True,
        300,
        300,
    )
    del world.s3.state.put_faults[key]
    with pytest.raises(StorageKeyPendingError):
        await world.backend.write_bytes(key, b"b" * 100)
    assert (world.stored(key), await world.usage(agent)) == (b"a" * 300, 300)


async def test_a_5xx_is_unknown_so_the_key_stays_blocked_and_charged_until_something_proves_the_write(
    quota_world: StorageWorld,
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    world.s3.state.put_faults[key] = "reject_500"
    with pytest.raises(StorageKeyPendingError):
        await world.backend.write_bytes(key, b"a" * 300)
    del world.s3.state.put_faults[key]
    with pytest.raises(StorageKeyPendingError):
        await world.backend.write_bytes(key, b"a" * 300)
    with pytest.raises(StorageKeyPendingError):
        await world.backend.delete(key)
    with pytest.raises(StorageKeyPendingError):
        await world.backend.create_bytes_if_absent(key, b"a" * 300)
    assert (await world.usage(agent), world.stored(key)) == (300, None)
    await world.backend.write_bytes(world.key(agent, "other"), b"x" * 100)


async def test_a_definitive_rejection_releases_the_reservation(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    world.s3.state.put_faults[key] = "reject_403"
    with pytest.raises(ClientError):
        await world.backend.write_bytes(key, b"a" * 300)
    assert (await world.usage(agent), await world.row(agent)) == (0, None)
    del world.s3.state.put_faults[key]
    await world.backend.write_bytes(key, b"a" * 300)


async def test_an_ambiguous_delete_blocks_reuse_regardless_of_current_provider_contents(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    kept, gone = world.key(agent, "kept"), world.key(agent, "gone")
    for key in (kept, gone):
        await world.backend.write_bytes(key, b"x" * 200)
    world.s3.state.delete_faults[kept] = "reject_500"
    world.s3.state.delete_faults[gone] = "apply_then_500"
    for key in (kept, gone):
        with pytest.raises(StorageKeyPendingError):
            await world.backend.delete(key)
    assert await world.usage(agent) == 400
    world.s3.state.delete_faults.clear()
    for key in (kept, gone):
        with pytest.raises(StorageKeyPendingError):
            await world.backend.delete(key)
        with pytest.raises(StorageKeyPendingError):
            await world.backend.write_bytes(key, b"new")
    assert (await world.usage(agent), world.stored(gone), world.stored(kept)) == (400, None, b"x" * 200)


async def test_a_cancelled_write_still_records_what_the_provider_stored(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    gate = world.s3.state.gates[key] = Gate(apply_before_hold=True)
    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tasks:

            async def write() -> None:
                await world.backend.write_bytes(key, b"x" * 300)

            tasks.start_soon(write)
            assert await anyio.to_thread.run_sync(gate.arrived.wait, 10)
            scope.cancel()
            gate.release.set()
    settled = await world.row(agent)
    assert settled is not None
    assert (world.stored(key), settled.pending, await world.usage(agent)) == (b"x" * 300, False, 300)


async def test_a_reservation_survives_a_restart_and_blocks_the_key_without_freeing_capacity(
    quota_world: StorageWorld,
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    reserved = await reserve_write(ManagedKey(agent, "workspace/uploads/report.bin"), 300, md5(b"x" * 300))
    assert isinstance(reserved, Reservation)
    conninfo = get_pool().conninfo
    assert isinstance(conninfo, str)
    await close_pool()
    _ = await init_pool(conninfo=conninfo, min_size=1, max_size=8)
    with pytest.raises(StorageKeyPendingError):
        await world.backend.write_bytes(key, b"x" * 300)
    await world.backend.write_bytes(world.key(agent, "b"), b"x" * 400)
    await world.backend.write_bytes(world.key(agent, "c"), b"x" * 300)
    assert await world.usage(agent) == 1000


async def test_a_stale_pending_charge_is_never_refunded_by_age(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    world.s3.state.put_faults[key] = "reject_500"
    with pytest.raises(StorageKeyPendingError):
        await world.backend.write_bytes(key, b"a" * 300)
    await world.scenario.db.execute("UPDATE agent_storage_objects SET updated_at = now() - interval '400 days'")
    del world.s3.state.put_faults[key]
    with pytest.raises(StorageKeyPendingError):
        await world.backend.write_bytes(key, b"a" * 300)
    assert await world.usage(agent) == 300


async def test_a_ledger_outage_refuses_a_paid_agents_write_before_the_provider(
    quota_world: StorageWorld, monkeypatch: pytest.MonkeyPatch
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)

    def offline() -> None:
        raise DbError("ledger offline")

    monkeypatch.setattr(quota_ledger, "durable_transaction", offline)
    with pytest.raises(StorageAccountingUnavailableError) as raised:
        await world.backend.write_bytes(key, b"a" * 100)
    assert raised.value.status_code == 503
    assert world.s3.state.count("PUT", key) == 0


async def test_an_ambiguous_tier_link_fails_closed(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    tenant = await world.tenant(OrganizationPlanId.PRO)
    _ = await approve_admission_for(world.scenario.db, tenant, OrganizationPlanId.MAX)
    agent = await world.scenario.agent("owner", tenant_id=tenant)
    with pytest.raises(StorageAccountingUnavailableError):
        await world.backend.write_bytes(world.key(agent), b"a" * 100)
    assert world.s3.state.count("PUT", world.key(agent)) == 0


async def test_a_write_succeeds_while_every_request_pool_connection_is_held(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    pool = get_pool()
    async with AsyncExitStack() as held:
        for _ in range(pool.max_size):
            _ = await held.enter_async_context(pool.connection())
        with anyio.fail_after(10):
            await world.backend.write_bytes(world.key(agent), b"a" * 100)
    assert await world.usage(agent) == 100


async def test_reserving_never_commits_the_enclosing_request_transaction(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    probe = {"id": world.scenario.tenant_id}

    async def request() -> None:
        async with connection_ctx() as db:
            await db.execute("UPDATE tenants SET name = 'uncommitted' WHERE id = %(id)s", probe)
            await world.backend.write_bytes(world.key(agent), b"a" * 100)
            raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        await request()
    name = await world.scenario.db.fetchval("SELECT name FROM tenants WHERE id = %(id)s", probe)
    assert (name, await world.usage(agent)) == ("Acme", 100)
