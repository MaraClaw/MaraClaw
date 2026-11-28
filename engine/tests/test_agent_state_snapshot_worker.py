"""The agent_state_snapshot loop: which agents it snapshots, under which locks, and what it publishes and prunes."""

import asyncio
import fcntl
import os
import shutil
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pywhales import DockerClient

from app.config import get_settings
from app.records.agent import AgentRecord
from app.services import (
    agent_state_snapshot as snapshots,
    agent_state_snapshot_tracker as tracking,
    agent_state_snapshot_worker as worker,
)
from app.services.agent_state_snapshot import (
    AGENT_DB,
    GUEST_STATE_DIR,
    ROLE_LABEL,
    agent_prefix,
    new_snapshot_name,
    parse_name,
    tenant_token,
    work_root,
)
from app.services.agent_state_snapshot_archive import ASSET_PAYLOAD
from app.services.agent_state_snapshot_tracker import SnapshotOutcome, SnapshotTracker
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError
from app.services.storage_runtime.base import ConditionalWriteResult, WriteCondition
from app.services.storage_runtime.upload_source import UploadSource
from host_disk_fakes import FLOOR, full_disk
from im_token_cache_fakes import captured_app_log
from state_snapshot_fakes import (
    ARCHIVE_ROOT,
    Clock,
    MemoryStore,
    ScriptedDocker,
    agent_database,
    backup_of,
    backup_report,
    crashed,
    flock_free,
    openclaw_state,
    write_file,
)
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]


class Agents:
    """The agents table as `agent_dao` reads it; a test may change `rows` between reads."""

    def __init__(self, *agents: AgentRecord) -> None:
        self.rows: dict[UUID, AgentRecord] = {agent.id: agent for agent in agents}
        self.reads: list[UUID] = []
        self.pages: list[UUID | None] = []

    async def get(self, agent_id: UUID) -> AgentRecord | None:
        self.reads.append(agent_id)
        return self.rows.get(agent_id)

    async def list_workspace_mirror_page(self, after_id: UUID | None, limit: int) -> list[AgentRecord]:
        self.pages.append(after_id)
        return sorted(
            (agent for agent in self.rows.values() if after_id is None or agent.id > after_id), key=lambda a: a.id
        )[:limit]


@dataclass
class World:
    root: Path
    agent: AgentRecord
    store: MemoryStore
    agents: Agents
    docker: ScriptedDocker
    tracker: SnapshotTracker

    @property
    def agent_dir(self) -> Path:
        return self.root / str(self.agent.id)

    def lock(self, suffix: str) -> Path:
        return self.root / "_setup_locks" / f"{self.agent.id}{suffix}"

    async def snapshot(self) -> SnapshotOutcome:
        return await worker.snapshot_agent(cast("DockerClient", self.docker), self.agent, self.tracker)

    def names(self) -> list[str]:
        prefix = agent_prefix(self.agent.id)
        return sorted(key.removeprefix(prefix) for key in self.store.objects if key.startswith(prefix))


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """One idle agent with OpenClaw state under an agents root, an empty S3 store and a backup that succeeds."""
    root = tmp_path / "agents"
    agent = AgentRecord(
        id=uuid4(), name="Snapshot", creator_id=uuid4(), tenant_id=uuid4(), openclaw_image_id="openclaw:pinned"
    )
    _ = openclaw_state(root / str(agent.id))
    store, agents = MemoryStore(), Agents(agent)

    async def allowed(_agent_id: UUID) -> None:
        return None

    monkeypatch.setattr(worker, "snapshot_store", lambda: store)
    monkeypatch.setattr(worker, "agent_state_dir", lambda agent_id: root / str(agent_id))
    monkeypatch.setattr(worker, "agents_root", lambda: root)
    monkeypatch.setattr(worker, "agent_dao", agents)
    monkeypatch.setattr(worker, "assert_no_active_run", allowed)
    monkeypatch.setattr(worker, "assert_transfer_ready", allowed)
    return World(root, agent, store, agents, ScriptedDocker(backup_of()), SnapshotTracker())


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The tracker's monotonic clock, stepped by the test."""
    clock = Clock()
    monkeypatch.setattr(tracking, "_monotonic", clock)
    return clock


def interval() -> float:
    return get_settings().AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS


async def test_a_changed_agent_is_backed_up_under_both_locks_then_published_and_pruned_under_the_snapshot_flock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given older snapshots of the agent: one plain, one held, one of a former tenant, and a note that is no snapshot.
    agent_id, tenant = world.agent.id, tenant_token(world.agent.tenant_id)
    old = new_snapshot_name("0" * 16, tenant, datetime(2026, 1, 1, tzinfo=UTC))
    held = new_snapshot_name("1" * 16, tenant, datetime(2026, 1, 2, tzinfo=UTC))
    former = new_snapshot_name("2" * 16, "none", datetime(2026, 1, 3, tzinfo=UTC))
    world.store.objects |= {name.key(agent_id): b"old" for name in (old, held, former)}
    world.store.objects |= {held.held_key(agent_id): b"", f"{agent_prefix(agent_id)}README": b"note"}
    locks: dict[str, tuple[bool, bool]] = {}
    backup, upload = world.docker.act, world.store.write_source_if_match

    def backing_up(mounts: dict[str, Path]) -> str:
        locks["backup"] = (flock_free(world.lock(".lock")), flock_free(world.lock(".snapshot.lock")))
        return backup(mounts)

    async def uploading(
        key: str, source: UploadSource, *, condition: WriteCondition | None = None, content_type: str | None = None
    ) -> ConditionalWriteResult:
        locks["upload"] = (flock_free(world.lock(".lock")), flock_free(world.lock(".snapshot.lock")))
        return await upload(key, source, condition=condition, content_type=content_type)

    world.docker.act = backing_up
    monkeypatch.setattr(world.store, "write_source_if_match", uploading)
    # When the worker snapshots the agent.
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED
    # Then turns were held off only during the backup; the snapshot flock covered the upload too.
    assert locks == {"backup": (False, False), "upload": (True, False)}
    # And one new snapshot was created only if absent, the older unheld ones pruned, the held one and the note kept.
    [(key, condition, content_type)] = world.store.writes
    parsed = parse_name(key.removeprefix(agent_prefix(agent_id)))
    assert parsed is not None
    new, is_marker = parsed
    assert (is_marker, new.tenant) == (False, tenant)
    assert (condition, content_type) == (WriteCondition(require_absent=True), "application/gzip")
    assert world.names() == sorted(
        [held.file_name, held.held_key(agent_id).rpartition("/")[2], new.file_name, "README"]
    )
    with tarfile.open(fileobj=BytesIO(world.store.objects[key])) as tar:
        assert f"{ARCHIVE_ROOT}/{ASSET_PAYLOAD}/{AGENT_DB}" in tar.getnames()
    # And the backup ran the agent's image offline in a removed, labelled one-off on the agent and a work directory.
    [run] = world.docker.runs
    assert (run.image, run.options["name"]) == ("openclaw:pinned", f"maraclaw-state-snapshot-{agent_id}")
    assert (run.options["networks"], run.options["pull"], run.options["remove"]) == (["none"], "never", True)
    assert run.options["labels"][ROLE_LABEL] == "create"
    assert run.options["envs"]["OPENCLAW_STATE_DIR"] == GUEST_STATE_DIR
    assert run.mounts[GUEST_STATE_DIR] == world.agent_dir
    assert run.mounts["/out"].is_relative_to(work_root(world.root) / "create")
    # And the work directory, which held the archive's secrets, is gone.
    assert list((work_root(world.root) / "create").iterdir()) == []


async def test_an_unchanged_agent_is_skipped_without_docker_also_by_a_restarted_worker(world: World) -> None:
    # Given a published snapshot.
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED
    # When the next pass, and a restarted worker that only lists the prefix once, find the same state.
    assert await world.snapshot() == SnapshotOutcome.UNCHANGED
    world.tracker = SnapshotTracker()
    listed = len(world.store.listed)
    assert await world.snapshot() == SnapshotOutcome.UNCHANGED
    # Then neither ran another backup.
    assert len(world.store.listed) == listed + 1
    assert len(world.docker.runs) == 1


@pytest.mark.parametrize("held", [".lock", ".snapshot.lock"])
async def test_a_held_setup_lock_or_snapshot_flock_skips_the_agent_this_pass(world: World, held: str) -> None:
    # Given a turn holding the setup lock, or a restore holding the snapshot flock.
    world.lock(held).parent.mkdir(parents=True, exist_ok=True)
    with world.lock(held).open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # When the worker reaches the agent, without waiting.
        async with asyncio.timeout(5):
            outcome = await world.snapshot()
    # Then it is skipped with nothing run or written, and the next pass snapshots it.
    assert (outcome, world.docker.runs, world.store.writes) == (SnapshotOutcome.BUSY, [], [])
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED


@pytest.mark.parametrize("state", ["no directory", "no databases", "deleted", "upgrading", "transfer pending"])
async def test_an_agent_without_state_or_eligibility_never_reaches_docker(
    world: World, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    # Given an agent that has no host state, lost its row, or is being upgraded or transferred.
    expected = SnapshotOutcome.INELIGIBLE

    async def upgrading(_agent_id: UUID) -> None:
        raise OpenClawUpdateInProgressError

    async def transferring(_agent_id: UUID) -> None:
        raise HTTPException(status_code=423, detail="transfer_cleanup_pending")

    match state:
        case "no directory":
            world.agent = AgentRecord(id=uuid4(), name="Fresh", creator_id=uuid4())
            expected = SnapshotOutcome.NO_STATE
        case "no databases":
            (world.agent_dir / AGENT_DB).unlink()
            (world.agent_dir / "state" / "openclaw.sqlite").unlink()
            expected = SnapshotOutcome.NO_STATE
        case "deleted":
            world.agents.rows.clear()
        case "upgrading":
            monkeypatch.setattr(worker, "assert_no_active_run", upgrading)
        case _:
            monkeypatch.setattr(worker, "assert_transfer_ready", transferring)
    # When the worker reaches it.
    assert await world.snapshot() == expected
    # Then no container ran, nothing was written, and no agent directory was created.
    assert (world.docker.runs, world.store.writes) == ([], [])
    assert world.agent_dir.exists() is (state != "no directory")


async def test_a_tenant_change_between_backup_and_upload_discards_the_snapshot(world: World) -> None:
    # Given a company transfer that moves the agent while its backup runs.
    backup = world.docker.act

    def transferred(mounts: dict[str, Path]) -> str:
        world.agents.rows[world.agent.id] = AgentRecord(
            id=world.agent.id, name=world.agent.name, creator_id=world.agent.creator_id, tenant_id=uuid4()
        )
        return backup(mounts)

    world.docker.act = transferred
    # When the worker finishes the backup.
    assert await world.snapshot() == SnapshotOutcome.INELIGIBLE
    # Then nothing was uploaded under the old tenant.
    assert (world.store.writes, world.names()) == ([], [])


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param("report", id="unverified backup"),
        pytest.param("mid-run", id="session mid-run"),
        pytest.param("too large", id="over the size limit"),
    ],
)
async def test_a_rejected_backup_keeps_the_previous_snapshot_and_is_retried_only_after_its_backoff(
    world: World, clock: Clock, monkeypatch: pytest.MonkeyPatch, spoil: str
) -> None:
    # Given a current snapshot, then a change whose backup must not be published.
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED
    previous = world.names()
    _ = (world.agent_dir / AGENT_DB).unlink()
    match spoil:
        case "report":
            _ = agent_database(world.agent_dir / AGENT_DB)
            world.docker.act = backup_of(backup_report(verified=False))
        case "mid-run":
            _ = agent_database(world.agent_dir / AGENT_DB, entries=[{"lifecycleRunId": "run-1"}])
        case _:
            _ = agent_database(world.agent_dir / AGENT_DB)
            monkeypatch.setattr(get_settings(), "AGENT_STATE_SNAPSHOT_MAX_BYTES", 64)
    # When the worker snapshots the change.
    assert await world.snapshot() == SnapshotOutcome.REJECTED
    # Then the previous snapshot stays current and the agent is left alone for two intervals.
    assert world.names() == previous
    clock.advance(2 * interval() - 1)
    assert await world.snapshot() == SnapshotOutcome.BACKING_OFF
    assert len(world.docker.runs) == 2
    # And once they have passed it is tried again.
    clock.advance(1)
    assert await world.snapshot() == SnapshotOutcome.REJECTED
    assert len(world.docker.runs) == 3


async def test_failures_back_off_twice_as_long_each_time_up_to_the_cap_and_a_published_snapshot_clears_it(
    world: World, clock: Clock
) -> None:
    # Given a backup container that keeps failing.
    world.docker.act = crashed()
    # When each retry comes the moment its backoff ends.
    for intervals in (2, 4, 8, 16, 24, 24):
        assert await world.snapshot() == SnapshotOutcome.FAILED
        # Then it waited twice as long as the retry before, up to 24 intervals.
        clock.advance(intervals * interval() - 1)
        assert await world.snapshot() == SnapshotOutcome.BACKING_OFF
        clock.advance(1)
    # And a published snapshot clears it: the next failure waits two intervals again.
    world.docker.act = backup_of()
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED
    _ = write_file(world.agent_dir / ".openclaw-gateway-token", b"rotated", mode=0o600)
    world.docker.act = crashed()
    assert await world.snapshot() == SnapshotOutcome.FAILED
    clock.advance(2 * interval() - 1)
    assert await world.snapshot() == SnapshotOutcome.BACKING_OFF
    clock.advance(1)
    assert await world.snapshot() == SnapshotOutcome.FAILED


async def test_an_unchanged_result_clears_the_backoff(world: World, clock: Clock) -> None:
    # Given a current snapshot and a change whose backup fails twice, so the next wait is eight intervals.
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED
    token = world.agent_dir / ".openclaw-gateway-token"
    original, stamp = token.read_bytes(), token.stat().st_mtime_ns
    _ = write_file(token, b"rotated", mode=0o600)
    world.docker.act = crashed()
    for intervals in (2, 4):
        assert await world.snapshot() == SnapshotOutcome.FAILED
        clock.advance(intervals * interval())
    # When the state goes back to what was published.
    _ = write_file(token, original, mode=0o600)
    os.utime(token, ns=(stamp, stamp))
    assert await world.snapshot() == SnapshotOutcome.UNCHANGED
    # Then a later failure waits two intervals, not eight.
    _ = write_file(token, b"rotated again", mode=0o600)
    assert await world.snapshot() == SnapshotOutcome.FAILED
    clock.advance(2 * interval())
    assert await world.snapshot() == SnapshotOutcome.FAILED


async def test_a_backing_off_agent_is_skipped_before_its_digest_and_before_either_lock(
    world: World, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an agent whose backup failed, and a turn holding its setup lock.
    world.docker.act = crashed()
    assert await world.snapshot() == SnapshotOutcome.FAILED
    digests: list[Path] = []
    digest = worker.state_digest

    def counting(agent_dir: Path, tenant: str) -> str:
        digests.append(agent_dir)
        return digest(agent_dir, tenant)

    monkeypatch.setattr(worker, "state_digest", counting)
    world.lock(".lock").parent.mkdir(parents=True, exist_ok=True)
    with world.lock(".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # When the worker reaches the agent inside its backoff.
        async with asyncio.timeout(5):
            outcome = await world.snapshot()
    # Then it is skipped as backing off: not busy, with no digest walked and no second container.
    assert (outcome, digests, len(world.docker.runs)) == (SnapshotOutcome.BACKING_OFF, [], 1)


async def test_an_ineligible_pass_does_not_start_a_backoff(world: World) -> None:
    # Given an agent whose row is gone.
    row = world.agents.rows.pop(world.agent.id)
    assert await world.snapshot() == SnapshotOutcome.INELIGIBLE
    # When its row is back, the very next pass snapshots it.
    world.agents.rows[row.id] = row
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED


async def test_a_busy_pass_does_not_extend_a_backoff_that_has_run_out(world: World, clock: Clock) -> None:
    # Given a failed backup and a backoff that has run out.
    world.docker.act = crashed()
    assert await world.snapshot() == SnapshotOutcome.FAILED
    clock.advance(2 * interval())
    # When a turn holds the setup lock at the next pass.
    world.lock(".lock").parent.mkdir(parents=True, exist_ok=True)
    with world.lock(".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert await world.snapshot() == SnapshotOutcome.BUSY
    # Then the pass after it is not delayed.
    world.docker.act = backup_of()
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED


@pytest.mark.parametrize("growth", ["before the backup", "while the backup runs"])
async def test_a_tree_past_the_walk_bound_is_rejected_unpublished_and_backed_off(
    world: World, clock: Clock, monkeypatch: pytest.MonkeyPatch, growth: str
) -> None:
    # Given a current snapshot and a change, in a tree past the walk bound before the backup or by its end.
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED
    previous, limit = world.names(), snapshots.WALK_MAX_ENTRIES
    _ = write_file(world.agent_dir / ".openclaw-gateway-token", b"rotated", mode=0o600)
    backup = world.docker.act

    def outgrown() -> None:
        monkeypatch.setattr(snapshots, "WALK_MAX_ENTRIES", 2)

    def growing(mounts: dict[str, Path]) -> str:
        outgrown()
        return backup(mounts)

    if growth == "before the backup":
        outgrown()
    else:
        world.docker.act = growing
    # When the worker snapshots the change.
    assert await world.snapshot() == SnapshotOutcome.REJECTED
    # Then nothing was published, no work directory is left, and the agent backs off instead of looking changed.
    assert world.names() == previous
    assert list((work_root(world.root) / "create").iterdir()) == []
    assert len(world.docker.runs) == (1 if growth == "before the backup" else 2)
    clock.advance(2 * interval() - 1)
    assert await world.snapshot() == SnapshotOutcome.BACKING_OFF
    # And once the tree fits the bound again, the next attempt publishes it.
    monkeypatch.setattr(snapshots, "WALK_MAX_ENTRIES", limit)
    world.docker.act = backup
    clock.advance(1)
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED


async def test_a_short_host_disk_fails_the_pass_before_any_scratch_directory_and_keeps_the_previous_snapshot(
    world: World, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a current snapshot, a change, and a host disk one byte short of its headroom.
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED
    previous, runs = world.names(), len(world.docker.runs)
    shutil.rmtree(work_root(world.root))
    _ = write_file(world.agent_dir / ".openclaw-gateway-token", b"rotated", mode=0o600)
    disk = full_disk(monkeypatch)
    # When the worker snapshots the change.
    assert await world.snapshot() == SnapshotOutcome.FAILED
    # Then no container ran and no scratch directory was created, and the previous snapshot stays current.
    assert (len(world.docker.runs), world.names(), work_root(world.root).exists()) == (runs, previous, False)
    assert disk.asked == [world.root]
    # And the agent backs off; once the disk has room again its next pass publishes.
    disk.free = FLOOR
    clock.advance(2 * interval())
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED
    assert world.names() != previous


def test_an_agent_left_without_a_snapshot_for_three_intervals_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(tracking, "_monotonic", clock)
    tracker, agent_id = SnapshotTracker(), uuid4()
    with captured_app_log() as lines:
        tracker.behind(agent_id)
        clock.advance(3 * interval())
        tracker.behind(agent_id)
        assert not any(str(agent_id) in line for line in lines)
        clock.advance(1)
        tracker.behind(agent_id)
        assert any(f"agent={agent_id} has had no current snapshot" in line for line in lines)
    tracker.current(agent_id)
    assert tracker.behind_since == {}


def test_budget_is_one_interval_plus_one_page_read_plus_one_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS", 600.0)
    assert worker.progress_budget_seconds() == 600.0 + worker.PAGE_READ_SECONDS + worker.AGENT_SECONDS


async def test_the_loop_clears_its_own_leftovers_then_snapshots_every_agent_and_beats(
    world: World, monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given leftovers of a crashed worker: a backup container and a work directory, beside a restore's work directory.
    create, restore = work_root(world.root) / "create" / "stale", work_root(world.root) / "restore" / "running"
    create.mkdir(parents=True)
    restore.mkdir(parents=True)
    world.docker.container.labelled[f"{ROLE_LABEL}=create"] = ["stale-backup"]
    second = AgentRecord(id=uuid4(), name="Stopped", creator_id=uuid4())
    world.agents.rows[second.id] = second
    monkeypatch.setattr(worker.agent_manager, "docker_client", world.docker)
    sleeps: list[float] = []

    async def paced(seconds: float) -> None:
        sleeps.append(seconds)
        await worker_http.pacing.wait(seconds)

    monkeypatch.setattr(worker, "sleep", paced)
    # When the loop starts and finishes its first pass.
    task = worker_http.start_loop(
        "agent_state_snapshot",
        worker.start_agent_state_snapshot_worker(),
        budget_seconds=worker.progress_budget_seconds(),
    )
    await worker_http.pacing.paused()
    # Then only the backup leftovers were removed, the agent with state was published, and the loop slept the interval.
    assert world.docker.container.removed == [("stale-backup", True)]
    assert (create.exists(), restore.exists()) == (False, True)
    assert len(world.names()) == 1
    assert not (world.root / str(second.id)).exists()
    assert world.agents.pages == [None]
    assert sleeps == [get_settings().AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS]
    worker_http.pacing.now += worker.progress_budget_seconds() - 1
    await worker_http.ready(expected=True)
    assert not task.done()
