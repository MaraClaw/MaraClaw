from pathlib import Path

import anyio
import pytest
from botocore.config import Config

from agent_deletion_fixtures import deletion_scenario
from app.services.storage_runtime import quota_s3
from app.services.storage_runtime.base import StorageVersion, WriteCondition
from app.services.storage_runtime.quota_errors import StorageAccountingUnavailableError, StorageKeyPendingError
from managed_storage_fixtures import StorageWorld, quota_world
from s3_wire_server import Gate

__all__ = ["deletion_scenario", "quota_world"]


@pytest.mark.parametrize("deleting", [False, True])
async def test_a_stale_version_cannot_change_the_current_object(quota_world: StorageWorld, deleting: bool):
    world = quota_world
    world.scale(1000, 400)
    agent = await world.agent()
    key = world.key(agent)
    await world.backend.write_bytes(key, b"old" * 100)
    previous = await world.backend.get_version(key)
    await world.backend.write_bytes(key, b"new" * 100)
    condition = WriteCondition(version_token=previous.token)
    result = (
        await world.backend.delete_if_match(key, condition=condition)
        if deleting
        else await world.backend.write_bytes_if_match(key, b"replacement", condition=condition)
    )
    assert (result.ok, result.conflict, world.stored(key), await world.usage(agent)) == (False, True, b"new" * 100, 300)


async def test_a_version_check_cannot_interleave_with_an_inflight_writer(quota_world: StorageWorld):
    world = quota_world
    world.scale(1000, 400)
    agent = await world.agent()
    key = world.key(agent)
    await world.backend.write_bytes(key, b"old" * 100)
    previous = await world.backend.get_version(key)
    gate = world.s3.state.gates[key] = Gate()
    async with anyio.create_task_group() as tasks:

        async def writer() -> None:
            await world.backend.write_bytes_if_match(
                key, b"new" * 100, condition=WriteCondition(version_token=previous.token)
            )

        tasks.start_soon(writer)
        assert await anyio.to_thread.run_sync(gate.arrived.wait, 10)
        try:
            with pytest.raises(StorageKeyPendingError):
                await world.backend.delete_if_match(key, condition=WriteCondition(version_token=previous.token))
        finally:
            gate.release.set()
    assert (world.stored(key), await world.usage(agent)) == (b"new" * 100, 300)


@pytest.mark.parametrize("deleting", [False, True])
async def test_an_absence_condition_preserves_an_occupied_key(quota_world: StorageWorld, deleting: bool):
    world = quota_world
    agent = await world.agent()
    key = world.key(agent)
    await world.backend.write_bytes(key, b"winner")
    condition = WriteCondition(require_absent=True)
    result = (
        await world.backend.delete_if_match(key, condition=condition)
        if deleting
        else await world.backend.write_bytes_if_match(key, b"loser", condition=condition)
    )
    assert (result.ok, result.conflict, world.stored(key), await world.usage(agent)) == (False, True, b"winner", 6)


async def test_a_timed_out_put_can_land_later_without_reusing_its_key_or_capacity(
    quota_world: StorageWorld, monkeypatch: pytest.MonkeyPatch
):
    world = quota_world
    world.scale(1000, 400)
    original = world.backend._boto_config()
    monkeypatch.setattr(world.backend, "_boto_config", lambda: original.merge(Config(read_timeout=0.2)))
    agent = await world.agent()
    key = world.key(agent)
    gate = world.s3.state.gates[key] = Gate()
    try:
        with pytest.raises(StorageKeyPendingError):
            await world.backend.write_bytes(key, b"late" * 75)
        assert gate.arrived.is_set()
        assert world.stored(key) is None
        assert not (await world.backend.get_version(key)).exists
        with pytest.raises(StorageKeyPendingError):
            await world.backend.write_bytes(key, b"replacement")
        await world.backend.write_bytes(world.key(agent, "b"), b"b" * 400)
        await world.backend.write_bytes(world.key(agent, "c"), b"c" * 300)
        assert (await world.usage(agent), world.s3.state.count("PUT", key)) == (1000, 1)
    finally:
        gate.release.set()


async def test_settlement_outage_returns_503_and_retains_the_committed_claim(
    quota_world: StorageWorld, monkeypatch: pytest.MonkeyPatch
):
    world = quota_world
    agent = await world.agent()
    key = world.key(agent)

    async def offline(*args: object) -> None:
        raise StorageAccountingUnavailableError

    monkeypatch.setattr(quota_s3, "settle", offline)
    with pytest.raises(StorageAccountingUnavailableError):
        await world.backend.write_bytes(key, b"stored")
    row = await world.row(agent)
    assert row is not None
    assert row.pending
    assert (world.stored(key), await world.usage(agent)) == (b"stored", 6)


async def test_an_overwrite_keeps_the_larger_charge_until_provider_success(quota_world: StorageWorld):
    world = quota_world
    world.scale(1000, 400)
    agent = await world.agent()
    key = world.key(agent)
    await world.backend.write_bytes(key, b"x" * 400)
    gate = world.s3.state.gates[key] = Gate()
    async with anyio.create_task_group() as tasks:

        async def shrink() -> None:
            await world.backend.write_bytes(key, b"y" * 100)

        tasks.start_soon(shrink)
        assert await anyio.to_thread.run_sync(gate.arrived.wait, 10)
        during = await world.usage(agent)
        gate.release.set()
        assert during == 400
    assert await world.usage(agent) == 100


async def test_fallback_conditional_delete_claims_the_key_before_checking_its_version(
    quota_world: StorageWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    world = quota_world
    agent = await world.agent()
    key = world.key(agent)
    await world.backend.write_bytes(key, b"original")
    version = await world.backend.get_version(key)
    arrived, release = anyio.Event(), anyio.Event()
    original = world.backend.get_version

    async def paused_version(target: str) -> StorageVersion:
        result = await original(target)
        arrived.set()
        await release.wait()
        return result

    monkeypatch.setattr(world.backend, "get_version", paused_version)
    async with anyio.create_task_group() as tasks:

        async def delete() -> None:
            result = await world.fallback(str(tmp_path)).delete_if_match(
                key, condition=WriteCondition(version_token=version.token)
            )
            assert result.ok

        tasks.start_soon(delete)
        await arrived.wait()
        try:
            with pytest.raises(StorageKeyPendingError):
                await world.backend.write_bytes(key, b"replacement")
        finally:
            release.set()
    assert (world.stored(key), await world.usage(agent)) == (None, 0)
