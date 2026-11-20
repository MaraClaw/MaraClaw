"""A member's bulk delete in S3 mode is not undone by the workspace mirror (``MARACLAW_TEST_POSTGRES_DSN``)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_deletion_fixtures import deletion_scenario
from app.api import background_tasks, files
from app.core import events
from app.services.storage_runtime import facade
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.workspace_mirror import ReconcileReport
from app.services.workspace_mirror_plan import MASS_DELETE_MIN
from app.services.workspace_mirror_scope import in_scope
from managed_storage_fixtures import quota_world
from workspace_mirror_fixtures import Mirror, mirror

__all__ = ["deletion_scenario", "mirror", "quota_world"]

BULK = [f"workspace/bulk/b{index:02}.txt" for index in range(24)]
SINGLES = ["workspace/single/one.txt", "workspace/single/two.txt"]
KEPT = ["workspace/keep.txt", "memory/notes.md", "skills/s/SKILL.md"]


@pytest.fixture
async def member_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Each test runs on its own event loop, so the route's workspace locks get a Redis client made on this one."""
    for name in ("_redis_client", "_cache_redis_client", "_redis_lock"):
        monkeypatch.setattr(events, name, None)
    yield
    await asyncio.gather(*tuple(background_tasks._tasks))
    await events.close_redis()


@pytest.fixture
async def member_storage(
    member_redis: None, mirror: Mirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FallbackStorageBackend:
    """What the member routes get from the facade in S3 mode with the mirror on: the managed primary over a local
    fallback rooted at the agents directory (each guest directory is <root>/<agent id>), which never promotes."""
    scenario = mirror.world.scenario
    # The member belongs to the scenario's company, so the agent does too.
    await scenario.db.execute(
        "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(agent)s",
        {"tenant": scenario.tenant_id, "agent": mirror.agent_id},
    )
    storage = FallbackStorageBackend(
        primary=mirror.world.backend, fallback=LocalStorageBackend(str(tmp_path)), promote=False
    )
    monkeypatch.setattr(facade, "_storage_backend", storage)
    return storage


async def member_delete(mirror: Mirror, storage: FallbackStorageBackend, path: str, *, versioned: bool) -> None:
    """One member delete through the route's own helper; a file carries the version token the member's listing showed."""
    token = (await storage.get_version(mirror.key(path))).token if versioned else None
    result = await files._delete_workspace_file(mirror.world.scenario["owner"], await mirror.agent(), path, token)
    assert result.ok, result.message


async def remaining(mirror: Mirror) -> tuple[set[str], set[str], set[str]]:
    """The mirrored paths left on the guest, in storage and in the manifest."""
    guest = {path.relative_to(mirror.agent_dir).as_posix() for path in mirror.agent_dir.rglob("*") if path.is_file()}
    prefix = f"{mirror.agent_id}/"
    stored = {key.removeprefix(prefix) for key in mirror.world.s3.state.objects if key.startswith(prefix)}
    return {rel for rel in guest if in_scope(rel)}, stored, set(await mirror.entries())


@pytest.mark.parametrize("by_tree", [True, False])
async def test_a_member_bulk_delete_is_not_undone_by_the_mirror(
    mirror: Mirror, member_storage: FallbackStorageBackend, by_tree: bool
):
    seeded = [*BULK, *SINGLES, *KEPT]
    for rel in seeded:
        _ = mirror.write(rel, rel.encode())
    base = await mirror.run()
    assert (base.pushed, await remaining(mirror)) == (len(seeded), ({*seeded}, {*seeded}, {*seeded}))
    # Far past the mirror's mass-delete threshold, so a delete it had to carry out itself would have been held back.
    deleted = [*BULK, *SINGLES]
    assert len(deleted) >= MASS_DELETE_MIN
    assert len(deleted) * 2 >= len(seeded)

    if by_tree:
        await member_delete(mirror, member_storage, "workspace/bulk", versioned=False)
    else:
        for rel in BULK:
            await member_delete(mirror, member_storage, rel, versioned=True)
    for rel in SINGLES:
        await member_delete(mirror, member_storage, rel, versioned=True)
    kept = {*KEPT}
    assert await remaining(mirror) == (kept, kept, {*seeded})
    writes = mirror.store_writes()

    passes: list[tuple[ReconcileReport, set[str], set[str], set[str]]] = []
    for _ in range(2):
        report = await mirror.run()
        guest, stored, manifest = await remaining(mirror)
        passes.append((report, guest, stored, manifest))

    # Nothing to delete either way, no guard, no copy back, a dropped row per deleted path.
    assert passes == [(ReconcileReport(had_base=True), kept, kept, kept)] * 2
    assert mirror.store_writes() == writes
    assert [mirror.read(rel) for rel in KEPT] == [rel.encode() for rel in KEPT]
