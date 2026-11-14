"""Managed S3 quota against live PostgreSQL and a loopback S3 provider (opt-in ``MARACLAW_TEST_POSTGRES_DSN``)."""

from uuid import uuid4

import anyio
import pytest
from botocore.exceptions import ClientError

from agent_deletion_fixtures import deletion_scenario
from app.records.organization_plan import OrganizationPlanId
from app.services.organization_plans import plan_for
from app.services.storage_runtime.quota_errors import (
    StorageFileTooLargeError,
    StorageKeyPendingError,
    StorageQuotaExceededError,
)
from managed_storage_fixtures import StorageWorld, quota_world
from tier_limit_fixtures import approve_admission_for

__all__ = ["deletion_scenario", "quota_world"]

LIMIT, CEILING = 1000, 400
MIB, GIB = 1024 * 1024, 1024**3
REAL_LIMITS = {
    OrganizationPlanId.PRO: (2 * GIB, 25 * MIB),
    OrganizationPlanId.MAX: (5 * GIB, 50 * MIB),
    OrganizationPlanId.ULTRA: (10 * GIB, 100 * MIB),
}


async def test_single_file_ceiling_is_exact_and_refusal_never_reaches_the_provider(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    await world.backend.write_bytes(world.key(agent), b"a" * CEILING)
    over = world.key(agent, "workspace/uploads/over.bin")
    with pytest.raises(StorageFileTooLargeError) as raised:
        await world.backend.write_bytes(over, b"a" * (CEILING + 1))
    assert raised.value.status_code == 413
    assert world.s3.state.count("PUT", over) == 0
    assert await world.usage(agent) == CEILING


async def test_total_allowance_fills_exactly_and_the_next_byte_is_refused(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    for name, size in (("a", CEILING), ("b", CEILING), ("c", LIMIT - 2 * CEILING)):
        await world.backend.write_bytes(world.key(agent, name), b"x" * size)
    assert await world.usage(agent) == LIMIT
    with pytest.raises(StorageQuotaExceededError) as raised:
        await world.backend.write_bytes(world.key(agent, "d"), b"x")
    assert raised.value.status_code == 507
    assert world.s3.state.count("PUT", world.key(agent, "d")) == 0


async def test_each_agent_has_its_own_allowance(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    tenant = await world.tenant(OrganizationPlanId.PRO)
    first = await world.scenario.agent("owner", tenant_id=tenant)
    second = await world.scenario.agent("owner", tenant_id=tenant)
    for name in ("a", "b"):
        await world.backend.write_bytes(world.key(first, name), b"x" * CEILING)
    await world.backend.write_bytes(world.key(first, "c"), b"x" * (LIMIT - 2 * CEILING))
    await world.backend.write_bytes(world.key(second, "a"), b"x" * CEILING)
    assert (await world.usage(first), await world.usage(second)) == (LIMIT, CEILING)


async def test_simultaneous_writers_cannot_overspend_the_last_capacity(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    outcomes: list[str] = []

    async def write(index: int) -> None:
        try:
            await world.backend.write_bytes(world.key(agent, f"file-{index}"), b"x" * 300)
        except StorageQuotaExceededError:
            outcomes.append("507")
        else:
            outcomes.append("ok")

    async with anyio.create_task_group() as tasks:
        for index in range(8):
            tasks.start_soon(write, index)
    assert sorted(outcomes) == ["507"] * 5 + ["ok"] * 3
    assert await world.usage(agent) == 900
    assert len(world.s3.state.objects) == 3


async def test_overwrite_charges_the_delta_and_shrinking_refunds(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    observed: list[int] = []
    for size in (300, 400, 100, 100):
        await world.backend.write_bytes(key, b"x" * size)
        observed.append(await world.usage(agent))
    assert observed == [300, 400, 100, 100]
    settled = await world.row(agent)
    assert settled is not None
    assert (settled.committed_bytes, settled.pending) == (100, False)


async def test_overwrite_growth_past_the_allowance_is_refused_and_leaves_the_object(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    for name, size in (("a", 400), ("b", 400), ("c", 200)):
        await world.backend.write_bytes(world.key(agent, name), bytes([97]) * size)
    with pytest.raises(StorageQuotaExceededError):
        await world.backend.write_bytes(world.key(agent, "c"), b"z" * 400)
    assert world.stored(world.key(agent, "c")) == bytes([97]) * 200
    assert await world.usage(agent) == LIMIT


async def test_an_agent_already_over_its_allowance_may_shrink_but_not_grow(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    for name in ("a", "b", "c"):
        await world.backend.write_bytes(world.key(agent, name), b"x" * 300)
    world.scale(500, CEILING)
    with pytest.raises(StorageQuotaExceededError):
        await world.backend.write_bytes(world.key(agent, "d"), b"x")
    await world.backend.write_bytes(world.key(agent, "a"), b"x" * 100)
    assert await world.usage(agent) == 700


async def test_create_conflict_keeps_the_winner_and_releases_the_loser(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    assert (await world.backend.create_bytes_if_absent(key, b"w" * 100)).ok
    loser = await world.backend.create_bytes_if_absent(key, b"L" * 300)
    assert (loser.ok, loser.conflict) == (False, True)
    assert world.stored(key) == b"w" * 100
    assert await world.usage(agent) == 100


async def test_conflict_with_an_unaccounted_object_charges_what_the_provider_holds(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    world.s3.state.objects[key] = b"p" * 50
    assert (await world.backend.create_bytes_if_absent(key, b"n" * 80)).conflict
    assert (world.stored(key), await world.usage(agent)) == (b"p" * 50, 50)


async def test_concurrent_creates_of_one_key_have_exactly_one_winner(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    wins: list[bool] = []

    async def create(size: int) -> None:
        try:
            wins.append((await world.backend.create_bytes_if_absent(key, b"x" * size)).ok)
        except StorageKeyPendingError:
            wins.append(False)

    async with anyio.create_task_group() as tasks:
        for size in (100, 200, 300):
            tasks.start_soon(create, size)
    stored = world.stored(key)
    assert stored is not None
    assert sorted(wins) == [False, False, True]
    assert await world.usage(agent) == len(stored)


async def test_delete_refunds_after_the_provider_confirms(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    await world.backend.write_bytes(key, b"x" * 300)
    await world.backend.delete(key)
    assert (world.stored(key), await world.usage(agent), await world.row(agent)) == (None, 0, None)


async def test_refused_delete_keeps_the_charge_and_the_object(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    await world.backend.write_bytes(key, b"x" * 300)
    world.s3.state.refused_deletes.add(key)
    with pytest.raises(ClientError):
        await world.backend.delete(key)
    row = await world.row(agent)
    assert (world.stored(key), await world.usage(agent)) == (b"x" * 300, 300)
    assert row is not None
    assert row.pending is False


async def test_agents_without_a_paid_tier_keep_their_unmetered_behavior(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent(tier=None)
    key = world.key(agent)
    await world.backend.write_bytes(key, b"x" * (CEILING * 5))
    await world.backend.delete(key)
    assert (world.stored(key), await world.usage(agent)) == (None, 0)


async def test_non_agent_keys_are_not_metered(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    key = f"enterprise_info_{uuid4()}/handbook.txt"
    await world.backend.write_bytes(key, b"x" * (CEILING * 5))
    assert len(world.stored(key) or b"") == CEILING * 5


async def test_limits_come_from_the_agents_current_tier_not_the_requesters_or_a_stored_snapshot(
    quota_world: StorageWorld,
):
    world = quota_world
    db = world.scenario.db
    _ = await approve_admission_for(db, world.scenario.tenant_id, OrganizationPlanId.ULTRA)
    agent = await world.agent(OrganizationPlanId.PRO)
    await db.execute("UPDATE organization_admission_requests SET plan = '{\"storage_limit_bytes\": 1}'::jsonb")
    ceiling = int(plan_for(OrganizationPlanId.PRO).max_file_size_bytes)
    with pytest.raises(StorageFileTooLargeError) as raised:
        await world.backend.write_bytes(world.key(agent), b"x" * (ceiling + 1))
    assert raised.value.limits["max_file_bytes"] == ceiling
    await world.backend.write_bytes(world.key(agent, "small"), b"x" * 2048)


async def test_accounting_survives_deletion_of_the_agent_row_until_the_objects_are_removed(
    quota_world: StorageWorld,
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    key = world.key(agent)
    await world.backend.write_bytes(key, b"x" * 300)
    await world.scenario.db.execute("DELETE FROM agents WHERE id = %(id)s", {"id": agent})
    assert await world.usage(agent) == 300
    await world.backend.delete(key)
    assert (await world.usage(agent), world.stored(key)) == (0, None)


@pytest.mark.parametrize("tier", list(REAL_LIMITS), ids=lambda tier: tier.value)
async def test_catalog_single_file_ceilings_are_exact_for_every_tier(
    quota_world: StorageWorld, tier: OrganizationPlanId
):
    world = quota_world
    _, ceiling = REAL_LIMITS[tier]
    agent = await world.agent(tier)
    over = world.key(agent, "over.bin")
    with pytest.raises(StorageFileTooLargeError) as raised:
        await world.backend.write_bytes(over, b"x" * (ceiling + 1))
    assert (raised.value.limits["max_file_bytes"], world.s3.state.count("PUT", over)) == (ceiling, 0)
    await world.backend.write_bytes(world.key(agent, "exact.bin"), b"x" * ceiling)
    assert (len(world.stored(world.key(agent, "exact.bin")) or b""), await world.usage(agent)) == (ceiling, ceiling)


@pytest.mark.parametrize("tier", list(REAL_LIMITS), ids=lambda tier: tier.value)
async def test_catalog_total_allowances_are_exact_for_every_tier(quota_world: StorageWorld, tier: OrganizationPlanId):
    world = quota_world
    limit, _ = REAL_LIMITS[tier]
    agent = await world.agent(tier)
    await world.scenario.db.execute(
        "INSERT INTO agent_storage_objects (agent_id, object_key, committed_bytes) VALUES (%(a)s, 'bulk', %(size)s)",
        {"a": agent, "size": limit - 2},
    )
    await world.scenario.db.execute(
        "INSERT INTO agent_storage_usage (agent_id, charged_bytes) VALUES (%(a)s, %(size)s)",
        {"a": agent, "size": limit - 2},
    )
    await world.backend.write_bytes(world.key(agent, "one"), b"x")
    await world.backend.write_bytes(world.key(agent, "two"), b"x")
    with pytest.raises(StorageQuotaExceededError) as raised:
        await world.backend.write_bytes(world.key(agent, "three"), b"x")
    assert (raised.value.limits["storage_limit_bytes"], await world.usage(agent)) == (limit, limit)
