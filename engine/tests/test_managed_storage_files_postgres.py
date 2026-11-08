import hashlib
from pathlib import Path

import anyio
import pytest

from agent_deletion_fixtures import deletion_scenario
from app.services.storage_runtime import upload_source
from app.services.storage_runtime.base import StorageConditionalRaceError
from app.services.storage_runtime.quota_errors import StorageFileTooLargeError, StorageKeyPendingError
from managed_storage_fixtures import StorageWorld, quota_world
from s3_wire_server import Gate, _Handler

__all__ = ["deletion_scenario", "quota_world"]


async def test_file_create_reserves_open_file_size_and_hash_before_provider_settles(
    quota_world: StorageWorld, tmp_path: Path
):
    world = quota_world
    world.scale(1000, 400)
    agent = await world.agent()
    key = world.key(agent)
    data = b"file data" * 30
    path = tmp_path / "source"
    path.write_bytes(data)
    gate = world.s3.state.gates[key] = Gate()

    async def create() -> None:
        assert (await world.backend.create_file_if_absent(key, path)).ok

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(create)
        try:
            assert await anyio.to_thread.run_sync(gate.arrived.wait, 10)
            row = await world.row(agent)
            assert row is not None
            assert (row.pending, row.reserved_bytes, await world.usage(agent)) == (True, len(data), len(data))
            assert row.expected_etag == hashlib.md5(data, usedforsecurity=False).hexdigest()
        finally:
            gate.release.set()
    row = await world.row(agent)
    assert row is not None
    assert (world.stored(key), row.committed_bytes, row.pending, await world.usage(agent)) == (
        data,
        len(data),
        False,
        len(data),
    )


async def test_file_create_refuses_oversize_before_provider(quota_world: StorageWorld, tmp_path: Path):
    world = quota_world
    world.scale(1000, 400)
    agent = await world.agent()
    key = world.key(agent)
    path = tmp_path / "large"
    path.write_bytes(b"x" * 401)
    with pytest.raises(StorageFileTooLargeError):
        await world.backend.create_file_if_absent(key, path)
    assert (world.s3.state.count("PUT", key), await world.usage(agent), await world.row(agent)) == (0, 0, None)


@pytest.mark.parametrize("failure", ["open", "hash"])
async def test_file_preparation_failure_does_not_reserve_or_block_key(
    quota_world: StorageWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    world = quota_world
    agent = await world.agent()
    key = world.key(agent)
    path = tmp_path / "source"
    if failure == "hash":
        path.write_bytes(b"data")

        def fail_hash(*args: object) -> str:
            raise OSError("hash failed")

        with monkeypatch.context() as patch:
            patch.setattr(upload_source, "_md5_of_prefix", fail_hash)
            with pytest.raises(OSError, match="hash failed"):
                await world.backend.create_file_if_absent(key, path)
    else:
        with pytest.raises(FileNotFoundError):
            await world.backend.create_file_if_absent(key, path)
    assert (world.s3.state.count("PUT", key), await world.usage(agent), await world.row(agent)) == (0, 0, None)
    path.write_bytes(b"recovered")
    assert (await world.backend.create_file_if_absent(key, path)).ok
    assert (world.stored(key), await world.usage(agent)) == (b"recovered", 9)


async def test_file_create_conflict_adopts_unaccounted_winner_size(quota_world: StorageWorld, tmp_path: Path):
    world = quota_world
    world.scale(1000, 400)
    agent = await world.agent()
    key = world.key(agent)
    world.s3.state.objects[key] = b"winner" * 10
    path = tmp_path / "source"
    path.write_bytes(b"loser" * 30)
    result = await world.backend.create_file_if_absent(key, path)
    row = await world.row(agent)
    assert row is not None
    assert (result.ok, result.conflict, world.stored(key), await world.usage(agent), row.pending) == (
        False,
        True,
        b"winner" * 10,
        60,
        False,
    )


async def test_file_create_exhausted_409_releases_charge(
    quota_world: StorageWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    world = quota_world
    agent = await world.agent()
    key = world.key(agent)
    path = tmp_path / "source"
    path.write_bytes(b"retry data")
    original = _Handler.do_PUT

    def conflict(handler: _Handler) -> None:
        target, _ = handler._target()
        if target != key:
            original(handler)
            return
        handler._read_body()
        with handler._state().lock:
            handler._state().calls.append(("PUT", target))
        handler._error(409, "ConditionalRequestConflict")

    with monkeypatch.context() as patch:
        patch.setattr(_Handler, "do_PUT", conflict)
        with pytest.raises(StorageConditionalRaceError):
            await world.backend.create_file_if_absent(key, path)
    assert (world.s3.state.count("PUT", key), await world.usage(agent), await world.row(agent)) == (3, 0, None)
    assert (await world.backend.create_file_if_absent(key, path)).ok
    assert world.stored(key) == b"retry data"


async def test_file_create_unknown_transport_outcome_remains_charged_and_blocks_key(
    quota_world: StorageWorld, tmp_path: Path
):
    world = quota_world
    agent = await world.agent()
    key = world.key(agent)
    path = tmp_path / "source"
    path.write_bytes(b"uncertain" * 20)
    world.s3.state.put_faults[key] = "apply_then_drop"
    with pytest.raises(StorageKeyPendingError):
        await world.backend.create_file_if_absent(key, path)
    del world.s3.state.put_faults[key]
    with pytest.raises(StorageKeyPendingError):
        await world.backend.create_file_if_absent(key, path)
    row = await world.row(agent)
    assert row is not None
    assert (row.pending, row.outcome_unknown, row.charged_bytes, await world.usage(agent)) == (True, True, 180, 180)
    assert (world.stored(key), world.s3.state.count("PUT", key)) == (b"uncertain" * 20, 1)


async def test_cancelled_file_create_waits_for_provider_and_settles(quota_world: StorageWorld, tmp_path: Path):
    world = quota_world
    agent = await world.agent()
    key = world.key(agent)
    path = tmp_path / "source"
    path.write_bytes(b"cancelled" * 20)
    gate = world.s3.state.gates[key] = Gate(apply_before_hold=True)
    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tasks:

            async def create() -> None:
                await world.backend.create_file_if_absent(key, path)

            tasks.start_soon(create)
            try:
                assert await anyio.to_thread.run_sync(gate.arrived.wait, 10)
                scope.cancel()
            finally:
                gate.release.set()
    row = await world.row(agent)
    assert row is not None
    assert (world.stored(key), row.pending, row.committed_bytes, await world.usage(agent)) == (
        b"cancelled" * 20,
        False,
        180,
        180,
    )
