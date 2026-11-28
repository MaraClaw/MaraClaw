"""Snapshot keys, the host state digest, the snapshot flock and the deletion of an agent's snapshot prefix."""

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError

from app.config import get_settings
from app.services import agent_state_snapshot as snapshots, workspace_mirror
from app.services.agent_state_snapshot import (
    AGENT_DB,
    SnapshotBusyError,
    SnapshotDeleteError,
    SnapshotName,
    agent_prefix,
    delete_agent_snapshots,
    has_host_state,
    list_snapshots,
    new_snapshot_name,
    parse_name,
    snapshot_lock,
    state_digest,
    tenant_token,
)
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.workspace_mirror_scope import WalkLimitError
from state_snapshot_fakes import MemoryStore, openclaw_state, write_file

TENANT = "5d0c1b8e-4f3a-4c55-9a51-1f0f4c1b2a3d"
DIGEST = "0123456789abcdef"
STAMP = "20261127T144439138000"


def _name(day: int, tenant: str = "none") -> SnapshotName:
    return new_snapshot_name(DIGEST, tenant, datetime(2026, 11, day, tzinfo=UTC))


@pytest.mark.parametrize("tenant_id", [UUID(TENANT), None])
def test_a_snapshot_name_round_trips_through_its_key_and_its_held_marker(tenant_id: UUID | None) -> None:
    # Given a snapshot name for an agent with a tenant, or with none.
    agent_id = uuid4()
    tenant = tenant_token(tenant_id)
    name = new_snapshot_name(DIGEST, tenant, datetime(2026, 11, 27, 14, 44, 39, 138000, tzinfo=UTC))
    # Then its key sits under the agent's prefix and parses back to it; its marker parses as held.
    assert name.key(agent_id) == f"state-snapshots/{agent_id}/{STAMP}Z_{DIGEST}_{tenant}.tar.gz"
    assert tenant == (TENANT if tenant_id else "none")
    assert parse_name(name.file_name) == (name, False)
    assert parse_name(name.held_key(agent_id).rpartition("/")[2]) == (name, True)


@pytest.mark.parametrize(
    "file_name",
    [
        "README",
        f"{STAMP}Z_{DIGEST}_{TENANT.upper()}.tar.gz",
        f"{STAMP}Z_{DIGEST[:15]}_none.tar.gz",
        f"20261327T144439138000Z_{DIGEST}_none.tar.gz",
        f"{STAMP}_{DIGEST}_none.tar.gz",
        f"{STAMP}Z_{DIGEST}_tenant.tar.gz",
        f"{STAMP}Z_{DIGEST}_none.tar.gz.held.bak",
    ],
)
def test_any_other_name_under_the_prefix_is_not_a_snapshot(file_name: str) -> None:
    assert parse_name(file_name) is None


async def test_the_listing_restores_the_newest_unheld_snapshot_of_a_tenant_and_prunes_only_older_unheld_ones() -> None:
    # Given an agent's snapshots, two of them held, one of a former tenant, and a note that is not a snapshot.
    agent_id = uuid4()
    oldest, held, former, newest_held = _name(1), _name(2), _name(3, TENANT), _name(4)
    store = MemoryStore({name.key(agent_id): b"archive" for name in (oldest, held, former, newest_held)})
    store.objects |= {held.held_key(agent_id): b"", newest_held.held_key(agent_id): b""}
    store.objects[f"{agent_prefix(agent_id)}README"] = b"operator note"
    store.objects[_name(5).key(uuid4())] = b"another agent"
    # When the prefix is listed.
    listing = await list_snapshots(store, agent_id)
    # Then only the agent's snapshots count, restores skip held ones, and a prune never touches a held one.
    assert set(listing.sizes) == {oldest, held, former, newest_held}
    assert listing.newest() == newest_held
    assert listing.restorable("none") == oldest
    assert listing.restorable(TENANT) == former
    assert listing.restorable(str(uuid4())) is None
    assert listing.superseded(_name(5)) == [oldest, former]
    assert store.listed == [agent_prefix(agent_id)]


def test_the_digest_follows_state_files_and_the_tenant_but_not_the_mirror_or_volatile_files(tmp_path: Path) -> None:
    # Given an agent directory with a symlink out of it.
    agent_dir = openclaw_state(tmp_path / "agent")
    outside = write_file(tmp_path / "outside" / "secret.json", b"outside")
    (agent_dir / "linked").symlink_to(outside.parent)
    base = state_digest(agent_dir, "none")
    # When only files snapshots leave to the mirror, volatile trees, shared memory and engine bookkeeping change.
    for rel in (
        "workspace/AGENTS.md",
        "memory/notes.md",
        "soul.md",
        "logs/gateway.log",
        "tmp/scratch",
        "plugin-skills/web/SKILL.md",
        "state/openclaw.sqlite-shm",
        ".maraclaw-rpc-port",
        ".maraclaw-mirror-generation",
    ):
        _ = write_file(agent_dir / rel, f"changed {rel}".encode())
    _ = outside.write_bytes(b"changed outside")
    # Then the digest is the same, and it differs for another tenant.
    assert state_digest(agent_dir, "none") == base
    assert state_digest(agent_dir, TENANT) != base


@pytest.mark.parametrize("change", ["database touched", "journal appeared", "token rewritten"])
def test_any_change_to_covered_state_changes_the_digest(tmp_path: Path, change: str) -> None:
    # Given an agent directory and its digest.
    agent_dir = openclaw_state(tmp_path / "agent")
    base = state_digest(agent_dir, "none")
    # When covered state changes.
    match change:
        case "database touched":
            stat = (agent_dir / AGENT_DB).stat()
            os.utime(agent_dir / AGENT_DB, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
        case "journal appeared":
            _ = write_file(agent_dir / "knowledge" / "registry.sqlite-journal", b"journal")
        case _:
            _ = write_file(agent_dir / ".openclaw-gateway-token", b"rotated-token", mode=0o600)
    # Then the digest changes.
    assert state_digest(agent_dir, "none") != base


@pytest.mark.parametrize(("bound", "limit"), [("WALK_MAX_ENTRIES", 2), ("WALK_SECONDS", 0.0)])
def test_a_tree_past_the_walk_bound_has_no_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str, limit: float
) -> None:
    agent_dir = openclaw_state(tmp_path / "agent")
    monkeypatch.setattr(snapshots, bound, limit)
    with pytest.raises(WalkLimitError):
        _ = state_digest(agent_dir, "none")


@pytest.mark.parametrize(
    ("planted", "expected"),
    [
        ("nothing", False),
        ("agent database", True),
        ("state database", True),
        ("symlinked database", False),
        ("database directory", False),
        ("symlinked parent", False),
    ],
)
def test_host_state_is_either_openclaw_database_as_a_regular_file(tmp_path: Path, planted: str, expected: bool) -> None:
    # Given an agent directory and real databases outside it.
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    outside = openclaw_state(tmp_path / "outside")
    match planted:
        case "agent database":
            _ = write_file(agent_dir / AGENT_DB, b"")
        case "state database":
            _ = write_file(agent_dir / "state" / "openclaw.sqlite", b"")
        case "symlinked database":
            (agent_dir / "state").mkdir()
            (agent_dir / "state" / "openclaw.sqlite").symlink_to(outside / "state" / "openclaw.sqlite")
        case "database directory":
            (agent_dir / "state" / "openclaw.sqlite").mkdir(parents=True)
        case "symlinked parent":
            (agent_dir / "state").symlink_to(outside / "state")
        case _:
            pass
    # Then only a regular file reached without a symlink counts.
    assert has_host_state(agent_dir) is expected


async def test_the_snapshot_flock_never_waits_and_never_creates_the_agent_directory(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agents" / str(uuid4())
    async with snapshot_lock(agent_dir):
        with pytest.raises(SnapshotBusyError):
            async with snapshot_lock(agent_dir):
                pass
    async with snapshot_lock(agent_dir):
        pass
    assert not agent_dir.exists()


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> tuple[UUID, MemoryStore]:
    """An S3 primary holding the agent's snapshots, another agent's and the agent's mirrored files; both switches off."""
    agent_id = uuid4()
    store = MemoryStore(
        {
            _name(1).key(agent_id): b"archive",
            _name(1).held_key(agent_id): b"",
            _name(2).key(uuid4()): b"another agent",
            f"{agent_id}/workspace/AGENTS.md": b"mirrored",
        }
    )
    monkeypatch.setattr(workspace_mirror, "get_storage_backend", lambda: store)
    monkeypatch.setattr(get_settings(), "WORKSPACE_MIRROR_ENABLED", False)
    monkeypatch.setattr(get_settings(), "AGENT_STATE_SNAPSHOT_ENABLED", False)
    return agent_id, store


async def test_deleting_an_agents_snapshots_ignores_both_switches_and_keeps_everything_else(
    tmp_path: Path, stored: tuple[UUID, MemoryStore]
) -> None:
    agent_id, store = stored
    kept = {key: data for key, data in store.objects.items() if not key.startswith(agent_prefix(agent_id))}
    await delete_agent_snapshots(tmp_path / "agents" / str(agent_id), agent_id)
    assert store.objects == kept


async def test_deleting_while_a_snapshot_or_restore_runs_is_busy_and_deletes_nothing(
    tmp_path: Path, stored: tuple[UUID, MemoryStore]
) -> None:
    agent_id, store = stored
    agent_dir = tmp_path / "agents" / str(agent_id)
    before = dict(store.objects)
    async with snapshot_lock(agent_dir):
        with pytest.raises(SnapshotBusyError):
            await delete_agent_snapshots(agent_dir, agent_id)
    assert store.objects == before


async def test_a_storage_failure_deleting_snapshots_is_a_delete_error(
    tmp_path: Path, stored: tuple[UUID, MemoryStore]
) -> None:
    agent_id, store = stored
    store.failures["delete"] = ClientError({"Error": {"Code": "InternalError", "Message": "S3"}}, "DeleteObjects")
    with pytest.raises(SnapshotDeleteError):
        await delete_agent_snapshots(tmp_path / "agents" / str(agent_id), agent_id)


async def test_local_storage_has_no_snapshots_to_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_mirror, "get_storage_backend", lambda: LocalStorageBackend(str(tmp_path / "local")))
    agent_id = uuid4()
    await delete_agent_snapshots(tmp_path / "agents" / str(agent_id), agent_id)
    assert not (tmp_path / "agents").exists()
