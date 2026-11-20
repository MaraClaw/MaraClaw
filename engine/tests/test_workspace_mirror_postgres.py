"""Workspace mirror passes against live PostgreSQL and the loopback S3 provider (``MARACLAW_TEST_POSTGRES_DSN``)."""

import hashlib
import itertools
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from agent_deletion_fixtures import deletion_scenario
from app.dao.agent_dao import agent_dao
from app.dao.workspace_mirror_entry_dao import workspace_mirror_entry_dao
from app.records.agent import AgentRecord
from app.records.workspace_mirror_entry import WorkspaceMirrorEntryRecord
from app.services import workspace_mirror
from app.services.workspace_mirror import (
    MirrorBusyError,
    MirrorConflictError,
    MirrorMode,
    MirrorReconcileError,
    ReconcileReport,
    SyncedFile,
    mirror_agent,
    mirror_lock,
    reconcile_agent,
    sync_path,
)
from app.services.workspace_mirror_plan import conflict_path
from managed_storage_fixtures import StorageWorld, quota_world

__all__ = ["deletion_scenario", "quota_world"]

SECOND_NS = 1_000_000_000
AGED_NS = 60 * SECOND_NS
HOUR_NS = 3600 * SECOND_NS


@dataclass
class Clock:
    now_ns: int

    def __call__(self) -> int:
        return self.now_ns


@dataclass
class Mirror:
    world: StorageWorld
    agent_id: UUID
    agent_dir: Path
    clock: Clock

    def key(self, rel: str) -> str:
        return f"{self.agent_id}/{rel}"

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.now_ns / SECOND_NS, UTC)

    def write(self, rel: str, data: bytes, *, age_ns: int = AGED_NS) -> Path:
        """A guest write whose mtime sits `age_ns` before the mirror's clock."""
        path = self.agent_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(data)
        os.utime(path, ns=(self.clock.now_ns - age_ns, self.clock.now_ns - age_ns))
        return path

    def read(self, rel: str) -> bytes | None:
        path = self.agent_dir / rel
        return path.read_bytes() if path.exists() else None

    def stored(self, rel: str) -> bytes | None:
        return self.world.stored(self.key(rel))

    async def put(self, rel: str, data: bytes) -> None:
        await self.world.backend.write_bytes(self.key(rel), data)

    async def run(
        self, mode: MirrorMode = MirrorMode.FULL, *, budget_seconds: float = workspace_mirror.PASS_BUDGET_SECONDS
    ) -> ReconcileReport:
        return await reconcile_agent(
            self.agent_id, self.agent_dir, self.world.backend, mode=mode, budget_seconds=budget_seconds
        )

    async def entries(self) -> dict[str, WorkspaceMirrorEntryRecord]:
        return await workspace_mirror_entry_dao.list_for_agent(self.agent_id)

    async def agent(self) -> AgentRecord:
        record = await agent_dao.get(self.agent_id)
        assert record is not None
        return record

    def store_writes(self) -> int:
        state = self.world.s3.state
        return state.count("PUT") + state.count("DELETE") + state.count("MULTIDELETE")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
async def mirror(quota_world: StorageWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mirror:
    agent_id = await quota_world.agent()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    # Ten minutes ahead: every write a test makes has settled, while the hour-long too-large back-off still holds.
    clock = Clock(time.time_ns() + 600 * SECOND_NS)
    monkeypatch.setattr(workspace_mirror, "_wall_clock_ns", clock)
    monkeypatch.setattr(workspace_mirror, "durable_store", lambda: quota_world.backend)
    monkeypatch.setattr(workspace_mirror, "agent_state_dir", lambda agent: tmp_path / str(agent))
    return Mirror(quota_world, agent_id, agent_dir, clock)


async def test_a_new_guest_file_is_uploaded(mirror: Mirror):
    _ = mirror.write("workspace/a.txt", b"alpha")

    report = await mirror.run()

    entry = (await mirror.entries())["workspace/a.txt"]
    assert (report.pushed, mirror.stored("workspace/a.txt"), entry.content_sha256) == (1, b"alpha", _sha(b"alpha"))


async def test_a_new_stored_object_reaches_the_guest(mirror: Mirror):
    await mirror.put("memory/notes.md", b"beta")

    report = await mirror.run()

    assert (report.pulled, mirror.read("memory/notes.md")) == (1, b"beta")


async def test_a_guest_edit_is_uploaded(mirror: Mirror):
    _ = mirror.write("workspace/a.txt", b"alpha")
    _ = await mirror.run()
    _ = mirror.write("workspace/a.txt", b"alpha, edited", age_ns=AGED_NS // 2)

    report = await mirror.run()

    assert (report.pushed, mirror.stored("workspace/a.txt")) == (1, b"alpha, edited")


async def test_a_storage_edit_reaches_the_guest(mirror: Mirror):
    _ = mirror.write("workspace/a.txt", b"alpha")
    _ = await mirror.run()
    await mirror.put("workspace/a.txt", b"stored edit")

    report = await mirror.run()

    assert (report.pulled, mirror.read("workspace/a.txt")) == (1, b"stored edit")


async def test_a_guest_delete_deletes_the_object(mirror: Mirror):
    path = mirror.write("workspace/a.txt", b"alpha")
    _ = await mirror.run()
    path.unlink()

    report = await mirror.run()

    assert (report.deleted_remote, mirror.stored("workspace/a.txt"), await mirror.entries()) == (1, None, {})


async def test_a_storage_delete_deletes_the_guest_file(mirror: Mirror):
    _ = mirror.write("workspace/a.txt", b"alpha")
    _ = mirror.write("soul.md", b"soul")
    _ = await mirror.run()
    await mirror.world.backend.delete(mirror.key("workspace/a.txt"))

    report = await mirror.run()

    assert (report.deleted_local, mirror.read("workspace/a.txt"), list(await mirror.entries())) == (
        1,
        None,
        ["soul.md"],
    )


async def test_an_empty_storage_listing_holds_the_whole_pass(mirror: Mirror):
    for rel in ("workspace/a.txt", "soul.md"):
        _ = mirror.write(rel, rel.encode())
    _ = await mirror.run()
    for rel in ("workspace/a.txt", "soul.md"):
        await mirror.world.backend.delete(mirror.key(rel))
    _ = mirror.write("workspace/new.txt", b"new")
    writes = mirror.store_writes()

    reports = [await mirror.run(), await mirror.run(MirrorMode.PULL_ONLY)]

    assert [(r.remote_missing_guarded, r.deleted_local, r.pushed) for r in reports] == [(True, 0, 0)] * 2
    assert (mirror.read("workspace/a.txt"), mirror.read("soul.md"), mirror.store_writes() - writes) == (
        b"workspace/a.txt",
        b"soul.md",
        0,
    )


async def test_edits_on_both_sides_keep_storage_in_place_and_the_guest_bytes_as_a_conflict_copy(mirror: Mirror):
    rel = "workspace/plan.md"
    _ = mirror.write(rel, b"v1")
    _ = await mirror.run()
    _ = mirror.write(rel, b"guest v2", age_ns=AGED_NS // 2)
    await mirror.put(rel, b"stored v2")

    report = await mirror.run()

    copy = conflict_path(rel, mirror.now())
    assert (report.conflicts, mirror.read(rel), mirror.stored(rel), mirror.read(copy)) == (
        1,
        b"stored v2",
        b"stored v2",
        b"guest v2",
    )
    assert ((await mirror.run()).pushed, mirror.stored(copy)) == (1, b"guest v2")


async def test_without_a_base_differing_sides_follow_the_conflict_rule(mirror: Mirror):
    rel = "workspace/plan.md"
    _ = mirror.write(rel, b"guest")
    await mirror.put(rel, b"stored")

    report = await mirror.run()

    copy = conflict_path(rel, mirror.now())
    assert (report.conflicts, mirror.read(rel), mirror.read(copy)) == (1, b"stored", b"guest")


async def test_a_wiped_directory_deletes_nothing_from_storage(mirror: Mirror):
    _ = mirror.write("workspace/a.txt", b"alpha")
    _ = mirror.write("skills/s/SKILL.md", b"skill")
    _ = await mirror.run()
    shutil.rmtree(mirror.agent_dir)
    mirror.agent_dir.mkdir()
    writes = mirror.store_writes()

    report = await mirror.run()

    assert (report.had_base, report.deleted_remote, report.pulled, mirror.store_writes() - writes) == (False, 0, 2, 0)
    assert (mirror.read("workspace/a.txt"), mirror.read("skills/s/SKILL.md")) == (b"alpha", b"skill")


async def test_mass_guest_deletes_are_held_back_and_restored(mirror: Mirror):
    paths = [f"workspace/f{index:02}.txt" for index in range(20)]
    for rel in paths:
        _ = mirror.write(rel, rel.encode())
    _ = await mirror.run()
    for rel in paths:
        (mirror.agent_dir / rel).unlink()

    report = await mirror.run()

    assert (report.had_base, report.mass_delete_guarded, report.deleted_remote, report.pulled) == (True, True, 0, 20)
    assert [mirror.read(rel) for rel in paths] == [rel.encode() for rel in paths]


async def test_mass_storage_deletes_stay_held_while_the_guest_restores_storage_over_several_passes(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch
):
    paths = [f"workspace/f{index:02}.txt" for index in range(20)]
    for rel in paths:
        _ = mirror.write(rel, rel.encode())
    _ = await mirror.run()
    for rel in paths:
        await mirror.world.backend.delete(mirror.key(rel))
    await mirror.put("workspace/uploaded.txt", b"uploaded")

    held = await mirror.run(MirrorMode.PULL_ONLY)
    ticks = itertools.count(step=100)
    # Every budget check moves the pass clock 100 seconds, so a 200-second pass uploads a single file.
    monkeypatch.setattr(workspace_mirror, "_monotonic", lambda: float(next(ticks)))
    cut = await mirror.run(budget_seconds=200)
    monkeypatch.setattr(workspace_mirror, "_monotonic", time.monotonic)
    rest = await mirror.run()

    assert [(r.mass_delete_guarded, r.deleted_local, r.pushed) for r in (held, cut, rest)] == [
        (True, 0, 0),
        (False, 0, 1),
        (False, 0, 19),
    ]
    assert [(mirror.read(rel), mirror.stored(rel)) for rel in paths] == [(rel.encode(), rel.encode()) for rel in paths]


async def test_a_restore_cut_short_by_its_budget_fails_until_a_pass_of_it_finishes(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch
):
    for rel in ("workspace/a.txt", "workspace/b.txt"):
        await mirror.put(rel, rel.encode())
    ticks = itertools.count(step=100)
    # Every budget check moves the pass clock 100 seconds, so a 200-second pass pulls a single file.
    monkeypatch.setattr(workspace_mirror, "_monotonic", lambda: float(next(ticks)))

    with pytest.raises(MirrorReconcileError) as cut:
        _ = await mirror.run(MirrorMode.PULL_ONLY, budget_seconds=200)
    finished = await mirror.run(MirrorMode.PULL_ONLY, budget_seconds=200)
    after = await mirror.run(MirrorMode.PULL_ONLY, budget_seconds=200)

    assert (cut.value.had_base, finished.had_base, finished.pulled, after.had_base) == (False, False, 1, True)
    assert (mirror.read("workspace/a.txt"), mirror.read("workspace/b.txt")) == (b"workspace/a.txt", b"workspace/b.txt")


async def test_a_guest_path_the_mirror_cannot_write_freezes_alone(mirror: Mirror):
    rel = f"workspace/{'n' * 240}.md"
    _ = mirror.write(rel, b"guest")
    await mirror.put(rel, b"stored")
    _ = mirror.write("workspace/b.txt", b"beta")

    report = await mirror.run()

    # The conflict copy's name runs past the file name limit, so only that path waits.
    assert (report.frozen, report.conflicts, report.pushed, mirror.read(rel), mirror.stored("workspace/b.txt")) == (
        1,
        0,
        1,
        b"guest",
        b"beta",
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory")
async def test_an_unreadable_guest_directory_freezes_only_the_paths_below_it(mirror: Mirror):
    _ = mirror.write("workspace/locked/a.txt", b"alpha")
    _ = await mirror.run()
    _ = mirror.write("workspace/b.txt", b"beta")
    locked = mirror.agent_dir / "workspace/locked"
    locked.chmod(0)
    try:
        report = await mirror.run()
    finally:
        locked.chmod(0o755)

    assert (report.frozen, report.deleted_remote, report.pushed) == (1, 0, 1)
    assert (mirror.stored("workspace/locked/a.txt"), mirror.stored("workspace/b.txt")) == (b"alpha", b"beta")


async def test_a_settling_guest_file_waits(mirror: Mirror):
    _ = mirror.write("workspace/a.txt", b"half written", age_ns=SECOND_NS)

    report = await mirror.run()

    assert (report.frozen, report.pushed, mirror.stored("workspace/a.txt"), await mirror.entries()) == (1, 0, None, {})


async def test_symlinks_and_hard_links_are_not_mirrored(mirror: Mirror, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    _ = outside.write_bytes(b"secret")
    (mirror.agent_dir / "workspace").mkdir()
    (mirror.agent_dir / "workspace/link.txt").symlink_to(outside)
    hard = mirror.write("workspace/hard.txt", b"linked")
    (mirror.agent_dir / "workspace/hard-too.txt").hardlink_to(hard)

    report = await mirror.run()

    names = ("workspace/link.txt", "workspace/hard.txt", "workspace/hard-too.txt")
    assert (report.pushed, report.frozen, [mirror.stored(rel) for rel in names]) == (0, 2, [None, None, None])


async def test_a_symlinked_scope_directory_freezes_instead_of_deleting(mirror: Mirror, tmp_path: Path):
    _ = mirror.write("workspace/a.txt", b"alpha")
    _ = await mirror.run()
    moved = tmp_path / "moved"
    (mirror.agent_dir / "workspace").rename(moved)
    (mirror.agent_dir / "workspace").symlink_to(moved)

    report = await mirror.run()

    assert (report.deleted_remote, report.frozen, mirror.stored("workspace/a.txt")) == (0, 1, b"alpha")
    assert list(await mirror.entries()) == ["workspace/a.txt"]


async def test_out_of_scope_files_and_objects_are_left_alone(mirror: Mirror):
    local_only = (".env", "openclaw.json", "workspace/.git/config", "workspace/node_modules/x.js", "memory/a.sqlite")
    for rel in local_only:
        _ = mirror.write(rel, b"local only")
    await mirror.put("uploads/report.bin", b"stored only")
    await mirror.put("workspace/.hidden", b"stored only")

    report = await mirror.run()

    assert (report, [mirror.stored(rel) for rel in local_only]) == (ReconcileReport(), [None] * len(local_only))
    assert (mirror.stored("uploads/report.bin"), mirror.read("workspace/.hidden")) == (b"stored only", None)


async def test_a_deleted_root_file_is_restored(mirror: Mirror):
    path = mirror.write("soul.md", b"I am")
    _ = await mirror.run()
    path.unlink()

    report = await mirror.run()

    assert (report.pulled, report.deleted_remote, mirror.read("soul.md"), mirror.stored("soul.md")) == (
        1,
        0,
        b"I am",
        b"I am",
    )


async def test_a_file_over_the_plan_cap_is_skipped_until_it_changes(mirror: Mirror):
    mirror.world.scale(10_000, 8)
    rel = "workspace/big.bin"
    _ = mirror.write(rel, b"x" * 16)

    first = await mirror.run()
    skipped = (await mirror.entries())[rel]
    unchanged = await mirror.run()
    _ = mirror.write(rel, b"small", age_ns=AGED_NS // 2)
    changed = await mirror.run()

    assert (first.too_large, skipped.skip_reason, unchanged.too_large, unchanged.pushed) == (1, "too_large", 0, 0)
    assert (changed.pushed, mirror.stored(rel), (await mirror.entries())[rel].skip_reason) == (1, b"small", None)


async def test_a_file_over_the_plan_cap_is_offered_again_after_an_hour(mirror: Mirror):
    mirror.world.scale(10_000, 8)
    _ = mirror.write("workspace/big.bin", b"x" * 16)
    _ = await mirror.run()
    mirror.clock.now_ns += 2 * HOUR_NS

    report = await mirror.run()

    assert (report.too_large, mirror.stored("workspace/big.bin")) == (1, None)


async def test_the_storage_limit_stops_uploads_but_not_downloads(mirror: Mirror):
    mirror.world.scale(10, 100)
    await mirror.put("workspace/r.txt", b"rr")
    for name, data in (("a", b"aaaaa"), ("b", b"bbbbb"), ("c", b"c")):
        _ = mirror.write(f"workspace/{name}.txt", data)

    report = await mirror.run()

    assert (report.quota_exhausted, report.pushed, report.pulled, mirror.read("workspace/r.txt")) == (
        True,
        1,
        1,
        b"rr",
    )
    assert [mirror.stored(f"workspace/{name}.txt") for name in "abc"] == [b"aaaaa", None, None]


async def test_pull_only_never_writes_to_storage(mirror: Mirror):
    _ = mirror.write("workspace/a.txt", b"alpha")
    gone = mirror.write("workspace/gone.txt", b"gone")
    _ = await mirror.run()
    _ = mirror.write("workspace/a.txt", b"alpha, edited", age_ns=AGED_NS // 2)
    _ = mirror.write("workspace/new.txt", b"new")
    gone.unlink()
    await mirror.put("workspace/r.txt", b"stored")
    writes = mirror.store_writes()

    report = await mirror.run(MirrorMode.PULL_ONLY)

    assert (report.pulled, report.pushed, report.deleted_remote, mirror.store_writes() - writes) == (1, 0, 0, 0)
    assert (mirror.read("workspace/r.txt"), mirror.stored("workspace/a.txt"), mirror.stored("workspace/new.txt")) == (
        b"stored",
        b"alpha",
        None,
    )


async def test_unchanged_bytes_under_a_new_stamp_are_recorded_without_an_upload(mirror: Mirror):
    path = mirror.write("workspace/a.txt", b"alpha")
    _ = await mirror.run()
    before = (await mirror.entries())["workspace/a.txt"]
    os.utime(path, ns=(mirror.clock.now_ns - AGED_NS // 2, mirror.clock.now_ns - AGED_NS // 2))
    puts = mirror.world.s3.state.count("PUT")

    report = await mirror.run()

    after = (await mirror.entries())["workspace/a.txt"]
    assert (report.pushed, mirror.world.s3.state.count("PUT") - puts) == (0, 0)
    assert (after.local_mtime_ns, after.content_sha256) == (mirror.clock.now_ns - AGED_NS // 2, before.content_sha256)


async def test_sync_path_uploads_a_settling_guest_file_at_once(mirror: Mirror):
    rel = "workspace/out/report.txt"
    _ = mirror.write(rel, b"report", age_ns=SECOND_NS)

    synced = await sync_path(await mirror.agent(), rel)

    assert (synced, mirror.stored(rel)) == (SyncedFile(rel, mirror.key(rel), 6), b"report")


async def test_sync_path_brings_a_newer_stored_version_to_the_guest_first(mirror: Mirror):
    rel = "workspace/out/report.txt"
    _ = mirror.write(rel, b"report")
    _ = await mirror.run()
    await mirror.put(rel, b"report, revised")

    synced = await sync_path(await mirror.agent(), rel)

    assert (synced.size_bytes, mirror.read(rel)) == (len(b"report, revised"), b"report, revised")


async def test_sync_path_reports_a_conflict_with_the_saved_copy(mirror: Mirror):
    rel = "workspace/out/report.txt"
    _ = mirror.write(rel, b"v1")
    _ = await mirror.run()
    _ = mirror.write(rel, b"guest v2", age_ns=AGED_NS // 2)
    await mirror.put(rel, b"stored v2")

    with pytest.raises(MirrorConflictError) as caught:
        _ = await sync_path(await mirror.agent(), rel)

    copy = conflict_path(rel, mirror.now())
    assert (caught.value.conflict_path, mirror.read(rel), mirror.read(copy)) == (copy, b"stored v2", b"guest v2")


@pytest.mark.parametrize(("listed", "error"), [((), MirrorBusyError), (("workspace/other.txt",), FileNotFoundError)])
async def test_sync_path_neither_deletes_nor_uploads_a_file_whose_stored_copy_vanished(
    mirror: Mirror, listed: tuple[str, ...], error: type[Exception]
):
    rel = "workspace/a.txt"
    _ = mirror.write(rel, b"alpha")
    _ = await mirror.run()
    await mirror.world.backend.delete(mirror.key(rel))
    for other in listed:
        await mirror.put(other, b"other")

    with pytest.raises(error):
        _ = await sync_path(await mirror.agent(), rel)

    assert (mirror.read(rel), mirror.stored(rel)) == (b"alpha", None)


async def test_mirror_agent_runs_a_full_pass_under_the_agent_locks(mirror: Mirror):
    _ = mirror.write("workspace/a.txt", b"alpha")

    report = await mirror_agent(await mirror.agent())

    assert report is not None
    assert (report.pushed, mirror.stored("workspace/a.txt")) == (1, b"alpha")


async def test_mirror_agent_skips_an_agent_whose_mirror_lock_is_held(mirror: Mirror):
    _ = mirror.write("workspace/a.txt", b"alpha")

    async with mirror_lock(mirror.agent_dir, wait_seconds=0):
        report = await mirror_agent(await mirror.agent())

    assert (report, mirror.stored("workspace/a.txt")) == (None, None)
