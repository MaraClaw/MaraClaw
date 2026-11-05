"""A policy-changing transfer and concurrent writers share one per-agent lock (opt-in ``MARACLAW_TEST_POSTGRES_DSN``)."""

import anyio
import pytest

from agent_deletion_fixtures import deletion_scenario
from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.records.organization_plan import OrganizationPlanId
from app.services.storage_runtime.quota_errors import StorageQuotaExceededError, StorageTransferBlockedError
from app.services.storage_runtime.quota_transfer import require_destination_storage
from managed_storage_fixtures import StorageWorld, quota_world
from s3_wire_server import Gate

__all__ = ["deletion_scenario", "quota_world"]

TIERS = {OrganizationPlanId.ULTRA: (1000, 400), OrganizationPlanId.PRO: (500, 400)}


async def wait_for_lock_waiter(db: DbConnection) -> None:
    """Return once a session is blocked on a row lock of the usage table."""
    with anyio.fail_after(10):
        while not await db.fetchval(  # noqa: ASYNC110 - PostgreSQL has no wait-event subscription
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND query ILIKE '%agent_storage_usage%' AND pid <> pg_backend_pid()"
        ):
            await anyio.sleep(0.02)


async def test_a_writer_that_arrives_during_a_transfer_reserves_against_the_destination_tier(
    quota_world: StorageWorld,
):
    world = quota_world
    world.scale(0, 0, per_tier=TIERS)
    agent = await world.agent(OrganizationPlanId.ULTRA)
    destination = await world.tenant(OrganizationPlanId.PRO)
    await world.backend.write_bytes(world.key(agent, "before"), b"x" * 300)
    outcome: list[BaseException | None] = []

    async def writer() -> None:
        try:
            await world.backend.write_bytes(world.key(agent, "during"), b"x" * 300)
        except StorageQuotaExceededError as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    async with anyio.create_task_group() as tasks, connection_ctx() as db:
        await require_destination_storage(destination, [agent])
        await db.execute("UPDATE agents SET tenant_id = %(t)s WHERE id = %(a)s", {"t": destination, "a": agent})
        tasks.start_soon(writer)
        await wait_for_lock_waiter(world.scenario.db)
        assert outcome == []
    assert [type(item) for item in outcome] == [StorageQuotaExceededError]
    assert (await world.usage(agent), world.stored(world.key(agent, "during"))) == (300, None)


async def test_a_reservation_committed_before_the_preflight_counts_against_the_destination(quota_world: StorageWorld):
    world = quota_world
    world.scale(0, 0, per_tier=TIERS)
    agent = await world.agent(OrganizationPlanId.ULTRA)
    destination = await world.tenant(OrganizationPlanId.PRO)
    await world.backend.write_bytes(world.key(agent, "before"), b"x" * 300)
    during = world.key(agent, "during")
    gate = world.s3.state.gates[during] = Gate()

    async def writer() -> None:
        await world.backend.write_bytes(during, b"x" * 300)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(writer)
        assert await anyio.to_thread.run_sync(gate.arrived.wait, 10)
        with pytest.raises(StorageTransferBlockedError) as raised:
            async with connection_ctx():
                await require_destination_storage(destination, [agent])
        gate.release.set()
        assert raised.value.limits["storage_used_bytes"] == 600
    assert await world.usage(agent) == 600


@pytest.mark.parametrize("sizes", [(300, 300), (401,)])
async def test_unlimited_tier_objects_block_a_paid_transfer_that_cannot_fit_them(
    quota_world: StorageWorld, sizes: tuple[int, ...]
):
    world = quota_world
    world.scale(0, 0, per_tier=TIERS)
    agent = await world.agent(tier=None)
    destination = await world.tenant(OrganizationPlanId.PRO)
    for index, size in enumerate(sizes):
        await world.backend.write_bytes(world.key(agent, str(index)), b"x" * size)
    with pytest.raises(StorageTransferBlockedError):
        async with connection_ctx():
            await require_destination_storage(destination, [agent])
    assert await world.usage(agent) == sum(sizes)
