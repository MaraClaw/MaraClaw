"""Tree deletion and read-through promotion under the managed quota (opt-in ``MARACLAW_TEST_POSTGRES_DSN``)."""

from pathlib import Path
from uuid import UUID

import pytest

from agent_deletion_fixtures import deletion_scenario
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
