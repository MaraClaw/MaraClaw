"""Tree deletion and read-through promotion under the managed quota (opt-in ``MARACLAW_TEST_POSTGRES_DSN``)."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import botocore.endpoint
import pytest

from agent_deletion_fixtures import deletion_scenario
from app.db.connection import DbConnection
from app.services.storage_runtime import quota_ledger
from app.services.storage_runtime.base import CopyRequest, StorageVersion
from app.services.storage_runtime.quota_errors import (
    ManagedKeyError,
    StorageDeleteIncompleteError,
    StorageFileTooLargeError,
    StorageKeyPendingError,
    StorageQuotaExceededError,
)
from app.services.storage_runtime.quota_keys import ManagedKey
from app.services.storage_runtime.quota_ledger import Reservation, reserve_write
from app.services.storage_runtime.quota_s3 import ManagedS3StorageBackend
from managed_storage_fixtures import StorageWorld, quota_world
from s3_wire_server import Throttle

__all__ = ["deletion_scenario", "quota_world"]

LIMIT, CEILING = 1000, 400


async def seed_tree(world: StorageWorld, agent: UUID, count: int, size: int = 10, folder: str = "tree") -> None:
    """Objects and their ledger rows as earlier facade writes would have left them."""
    for index in range(count):
        world.s3.state.objects[f"{agent}/{folder}/{index:05d}"] = b"x" * size
    await world.scenario.db.execute(
        "INSERT INTO agent_storage_objects (agent_id, object_key, committed_bytes) "
        "SELECT %(a)s, %(folder)s || '/' || lpad(g::text, 5, '0'), %(size)s FROM generate_series(0, %(last)s) g",
        {"a": agent, "folder": folder, "size": size, "last": count - 1},
    )
    await world.scenario.db.execute(
        "INSERT INTO agent_storage_usage (agent_id, charged_bytes) VALUES (%(a)s, %(total)s) "
        "ON CONFLICT (agent_id) DO UPDATE SET charged_bytes = agent_storage_usage.charged_bytes + %(total)s",
        {"a": agent, "total": count * size},
    )


async def test_deleting_a_tree_larger_than_one_listing_page_removes_and_refunds_every_object(
    quota_world: StorageWorld,
):
    world = quota_world
    agent = await world.agent()
    await seed_tree(world, agent, 2300)
    await seed_tree(world, agent, 3, folder="tree-sibling")
    world.s3.state.objects[f"{agent}/tree/untracked"] = b"u" * 7
    await world.backend.delete_tree(f"{agent}/tree")
    assert [key for key in world.s3.state.objects if key.startswith(f"{agent}/tree/")] == []
    assert sum(key.startswith(f"{agent}/tree-sibling/") for key in world.s3.state.objects) == 3
    assert await world.usage(agent) == 30
    assert world.s3.state.count("LIST") == 3


async def test_a_throttled_managed_tree_listing_retries_without_repeating_the_delete(
    quota_world: StorageWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = quota_world
    agent = await world.agent()
    await seed_tree(world, agent, 1)
    prefix = f"{agent}/tree"
    delays: list[float] = []
    monkeypatch.setattr(botocore.endpoint, "time", SimpleNamespace(sleep=delays.append))
    world.s3.state.throttles[("LIST", f"{prefix}/")] = Throttle(remaining=2)

    await world.backend.delete_tree(prefix)

    assert world.s3.state.count("LIST", f"{prefix}/") == 3
    assert world.s3.state.count("MULTIDELETE", f"{prefix}/") == 1
    assert len(delays) == 2
    assert world.s3.state.objects == {}
    assert await world.usage(agent) == 0


async def test_objects_the_provider_refuses_to_delete_stay_charged_and_the_call_reports_them(
    quota_world: StorageWorld,
):
    world = quota_world
    agent = await world.agent()
    await seed_tree(world, agent, 5)
    refused = {f"{agent}/tree/00001", f"{agent}/tree/00003"}
    world.s3.state.refused_deletes |= refused
    with pytest.raises(StorageDeleteIncompleteError) as raised:
        await world.backend.delete_tree(f"{agent}/tree")
    assert raised.value.limits["remaining_objects"] == 2
    assert set(world.s3.state.objects) == refused
    assert await world.usage(agent) == 20
    rows = await world.scenario.rows("SELECT object_key, operation_id FROM agent_storage_objects ORDER BY object_key")
    assert [(row["object_key"], row["operation_id"]) for row in rows] == [("tree/00001", None), ("tree/00003", None)]


async def test_an_ambiguous_batch_delete_keeps_every_charge_and_blocks_reuse(quota_world: StorageWorld):
    world = quota_world
    agent = await world.agent()
    await seed_tree(world, agent, 4)
    world.s3.state.multi_delete_status = 500
    with pytest.raises(StorageKeyPendingError) as raised:
        await world.backend.delete_tree(f"{agent}/tree")
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "storage_accounting_pending"
    assert (await world.usage(agent), len(world.s3.state.objects)) == (40, 4)
    world.s3.state.multi_delete_status = 200
    with pytest.raises(StorageDeleteIncompleteError):
        await world.backend.delete_tree(f"{agent}/tree")
    assert (await world.usage(agent), len(world.s3.state.objects)) == (40, 4)


async def test_a_tree_delete_leaves_objects_held_by_an_unresolved_write_charged(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    await seed_tree(world, agent, 3)
    held = await reserve_write(ManagedKey(agent, "tree/00001"), 300, "0" * 32)
    assert isinstance(held, Reservation)
    with pytest.raises(StorageDeleteIncompleteError):
        await world.backend.delete_tree(f"{agent}/tree")
    assert set(world.s3.state.objects) == {f"{agent}/tree/00001"}
    assert await world.usage(agent) == 300


async def test_tree_deletion_honors_a_configured_bucket_prefix(quota_world: StorageWorld):
    world = quota_world
    prefixed = ManagedS3StorageBackend(
        bucket="bucket",
        prefix="env/prod",
        endpoint_url=world.s3.endpoint,
        access_key_id="key",
        secret_access_key="secret",
    )
    agent = await world.agent()
    for name in ("dir/a", "dir/b", "keep"):
        await prefixed.write_bytes(f"{agent}/{name}", b"x" * 100)
    await prefixed.delete_tree(f"{agent}/dir")
    assert set(world.s3.state.objects) == {f"env/prod/{agent}/keep"}
    assert await world.usage(agent) == 100


async def test_whole_prefix_deletion_cannot_bypass_agent_accounting(quota_world: StorageWorld):
    with pytest.raises(ManagedKeyError):
        await quota_world.backend.delete_tree("")


@pytest.mark.parametrize("snapshot", [False, True])
async def test_promotion_of_a_legacy_file_is_charged_like_any_managed_write(
    quota_world: StorageWorld, tmp_path: Path, snapshot: bool
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    legacy = tmp_path / str(agent) / "workspace" / "uploads"
    legacy.mkdir(parents=True)
    _ = (legacy / "old.txt").write_bytes(b"o" * 300)
    storage = world.fallback(str(tmp_path))
    key = world.key(agent, "workspace/uploads/old.txt")
    data = (await storage.read_snapshot_bounded(key, 400)).data if snapshot else await storage.read_bytes(key)
    assert data == b"o" * 300
    assert (await world.usage(agent), world.stored(world.key(agent, "workspace/uploads/old.txt"))) == (300, b"o" * 300)


@pytest.mark.parametrize(
    ("size", "refusal"), [(CEILING + 1, StorageFileTooLargeError), (CEILING, StorageQuotaExceededError)]
)
@pytest.mark.parametrize("snapshot", [False, True])
async def test_a_promotion_that_does_not_fit_is_refused_without_a_provider_write(
    quota_world: StorageWorld, tmp_path: Path, size: int, refusal: type[Exception], snapshot: bool
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    for name in ("a", "b"):
        await world.backend.write_bytes(world.key(agent, name), b"x" * 400)
    await world.backend.write_bytes(world.key(agent, "c"), b"x" * 150)
    legacy = tmp_path / str(agent) / "workspace"
    legacy.mkdir(parents=True)
    _ = (legacy / "old.bin").write_bytes(b"o" * size)
    key = world.key(agent, "workspace/old.bin")
    storage = world.fallback(str(tmp_path))
    operation = storage.read_snapshot_bounded(key, 1000) if snapshot else storage.read_bytes(key)
    with pytest.raises(refusal):
        await operation
    assert world.s3.state.count("PUT", key) == 0


async def test_a_tree_delete_reports_a_pending_key_not_yet_visible_in_s3(quota_world: StorageWorld):
    world = quota_world
    agent = await world.agent()
    claim = await reserve_write(ManagedKey(agent, "tree/inflight"), 300, "0" * 32)
    assert isinstance(claim, Reservation)
    with pytest.raises(StorageDeleteIncompleteError):
        await world.backend.delete_tree(f"{agent}/tree")
    assert await world.usage(agent) == 300


async def listed(world: StorageWorld, agent: UUID, folder: str = "tree") -> list[StorageVersion]:
    """The versions a flat listing reports, as a folder delete or move takes them: no HEAD per file."""
    return [entry.version() async for entry in world.backend.list_files_flat(f"{agent}/{folder}")]


def count_ledger_transactions(world: StorageWorld) -> list[int]:
    count = [0]
    real = quota_ledger.durable_transaction

    @asynccontextmanager
    async def counted() -> AsyncGenerator[DbConnection]:
        count[0] += 1
        async with real() as db:
            yield db

    world.monkeypatch.setattr(quota_ledger, "durable_transaction", counted)
    return count


async def test_a_batch_delete_uses_two_ledger_transactions_per_thousand_files_and_spares_an_edited_one(
    quota_world: StorageWorld,
):
    world = quota_world
    agent = await world.agent()
    await seed_tree(world, agent, 2300)
    versions = await listed(world, agent)
    world.s3.state.objects[f"{agent}/tree/00007"] = b"edited after the listing"
    transactions = count_ledger_transactions(world)

    results = await world.backend.delete_batch_if_match(versions)

    assert transactions == [6]
    assert sum(result.ok for result in results.values()) == 2299
    edited = results[f"{agent}/tree/00007"]
    assert (edited.ok, edited.conflict, edited.current_version is not None and edited.current_version.exists) == (
        False,
        True,
        True,
    )
    assert set(world.s3.state.objects) == {f"{agent}/tree/00007"}
    assert world.s3.state.count("HEAD") == 0
    assert await world.usage(agent) == 10
    rows = await world.scenario.rows("SELECT object_key, operation_id FROM agent_storage_objects")
    assert [(row["object_key"], row["operation_id"]) for row in rows] == [("tree/00007", None)]


async def test_a_batch_delete_skips_a_key_held_by_an_unresolved_write(quota_world: StorageWorld):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    await seed_tree(world, agent, 3)
    versions = await listed(world, agent)
    held = await reserve_write(ManagedKey(agent, "tree/00001"), 300, "0" * 32)
    assert isinstance(held, Reservation)

    results = await world.backend.delete_batch_if_match(versions)

    assert {key.removeprefix(f"{agent}/tree/"): result.ok for key, result in results.items()} == {
        "00000": True,
        "00001": False,
        "00002": True,
    }
    assert set(world.s3.state.objects) == {f"{agent}/tree/00001"}
    assert await world.usage(agent) == 300


async def test_an_unanswered_batch_delete_keeps_every_charge_and_blocks_the_keys(quota_world: StorageWorld):
    world = quota_world
    agent = await world.agent()
    await seed_tree(world, agent, 3)
    versions = await listed(world, agent)
    world.s3.state.multi_delete_status = 500

    with pytest.raises(StorageKeyPendingError):
        _ = await world.backend.delete_batch_if_match(versions)

    assert (await world.usage(agent), len(world.s3.state.objects)) == (30, 3)


async def test_a_batch_copy_charges_every_destination_in_one_reservation_and_copies_server_side(
    quota_world: StorageWorld,
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    await seed_tree(world, agent, 5)
    versions = await listed(world, agent)
    world.s3.state.objects[f"{agent}/tree/00002"] = b"edited after the listing"
    transactions = count_ledger_transactions(world)

    results = await world.backend.copy_batch_if_match(
        [CopyRequest(version, version.key.replace("/tree/", "/moved/")) for version in versions]
    )

    assert transactions == [2]
    assert {key.removeprefix(f"{agent}/tree/"): result.ok for key, result in results.items()} == {
        "00000": True,
        "00001": True,
        "00002": False,
        "00003": True,
        "00004": True,
    }
    assert sorted(key.removeprefix(f"{agent}/moved/") for key in world.s3.state.objects if "/moved/" in key) == [
        "00000",
        "00001",
        "00003",
        "00004",
    ]
    assert (world.s3.state.count("GET"), world.s3.state.count("COPY")) == (0, 5)
    assert await world.usage(agent) == 90
    rows = await world.scenario.rows("SELECT operation_id FROM agent_storage_objects WHERE operation_id IS NOT NULL")
    assert rows == []


async def test_a_batch_copy_that_does_not_fit_is_refused_before_any_provider_call(quota_world: StorageWorld):
    world = quota_world
    world.scale(70, CEILING)
    agent = await world.agent()
    await seed_tree(world, agent, 5)
    versions = await listed(world, agent)

    with pytest.raises(StorageQuotaExceededError):
        _ = await world.backend.copy_batch_if_match(
            [CopyRequest(version, version.key.replace("/tree/", "/moved/")) for version in versions]
        )

    assert (world.s3.state.count("COPY"), await world.usage(agent)) == (0, 50)
