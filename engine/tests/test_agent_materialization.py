"""Storage rehydration never rewrites the files it reads from, yet still hydrates remote storage."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import override

import pytest

from app.services import agent_manager as module, agent_materialize
from app.services.agent_manager import AgentManager
from app.services.storage_runtime.base import MaterializedFile, StorageBackend, StorageEntry
from app.services.storage_runtime.local import LocalStorageBackend
from sqlite_guest_fixtures import running_sqlite_guest

DATABASE = "agents/main/agent/openclaw-agent.sqlite"

type LocalTree = tuple[AgentManager, LocalStorageBackend, uuid.UUID]


class RemoteStorage(StorageBackend):
    """Object storage with no local path: keys are listed and read, never aliased to a file."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects: dict[str, bytes] = objects

    @override
    async def exists(self, key: str) -> bool:
        return key in self.objects or await self.is_dir(key)

    @override
    async def is_dir(self, key: str) -> bool:
        return any(existing.startswith(key.rstrip("/") + "/") for existing in self.objects)

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        names = {
            existing.removeprefix(prefix).partition("/")[0] for existing in self.objects if existing.startswith(prefix)
        }
        return [
            StorageEntry(name=name, key=prefix + name, is_dir=await self.is_dir(prefix + name))
            for name in sorted(names)
        ]

    @override
    async def read_bytes(self, key: str) -> bytes:
        return self.objects[key]

    @override
    async def materialize(self, key: str) -> MaterializedFile | None:
        with tempfile.NamedTemporaryFile(delete=False) as file:
            file.write(self.objects[key])
        return MaterializedFile(Path(file.name), owned=True)

    @override
    async def list_files_flat(self, prefix: str) -> AsyncGenerator[StorageEntry]:
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.objects[key]))


class SwappingStorage(RemoteStorage):
    """Replaces a directory with a symlink once the engine has checked it, as a lingering guest process could."""

    def __init__(self, objects: dict[str, bytes], swap: Callable[[], None]) -> None:
        super().__init__(objects)
        self.swap: Callable[[], None] = swap

    @override
    async def materialize(self, key: str) -> MaterializedFile | None:
        self.swap()
        return await super().materialize(key)


@pytest.fixture
def local_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalTree:
    """A manager whose storage root is its own agents root, as with ``STORAGE_BACKEND=local``."""
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(module, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    return AgentManager.__new__(AgentManager), storage, uuid.uuid4()


def _database_files(database: Path) -> dict[str, tuple[bytes, int, int | None]]:
    """Content and identity of the database and WAL; the mmap-backed SHM keeps no mtime promise."""
    files = {name: database.with_name(database.name + name) for name in ("", "-wal", "-shm")}
    return {
        name: (path.read_bytes(), path.stat().st_ino, None if name == "-shm" else path.stat().st_mtime_ns)
        for name, path in files.items()
    }


async def test_local_materialization_leaves_a_live_sqlite_wal_untouched(local_tree: LocalTree) -> None:
    # Given a guest holding a WAL database open inside the tree that storage itself serves.
    manager, _storage, agent_id = local_tree
    database = Path(module.settings.STORAGE_LOCAL_ROOT) / str(agent_id) / DATABASE
    database.parent.mkdir(parents=True)
    with running_sqlite_guest(database) as guest:
        for _ in range(8):
            _ = guest.command("insert")
        before = _database_files(database)
        # When startup rehydrates the agent directory from that same local storage.
        _ = await manager._materialize_agent_dir(agent_id)
        # Then the database, WAL and SHM are the same files with the same bytes, and the guest keeps committing.
        assert _database_files(database) == before
        assert guest.command("insert") == "ok 9"


async def test_materialization_cannot_roll_back_commits_a_running_guest_makes(
    local_tree: LocalTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a guest that commits whenever the engine finishes reading one of its files.
    manager, storage, agent_id = local_tree
    database = Path(module.settings.STORAGE_LOCAL_ROOT) / str(agent_id) / DATABASE
    database.parent.mkdir(parents=True)
    with running_sqlite_guest(database) as guest:
        for _ in range(3):
            _ = guest.command("insert")
        read = storage.read_bytes
        commits = 0

        async def read_then_commit(key: str) -> bytes:
            nonlocal commits
            data = await read(key)
            commits += 1
            _ = guest.command("insert")
            return data

        monkeypatch.setattr(storage, "read_bytes", read_then_commit)
        # When the engine rehydrates the tree while that guest is still running.
        _ = await manager._materialize_agent_dir(agent_id)
        # Then every committed row survives and both the guest and a fresh reader see a sound database.
        assert guest.command("check") == f"ok {3 + commits}"
        with closing(sqlite3.connect(database)) as reader:
            assert reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert reader.execute("SELECT count(*) FROM events").fetchone() == (3 + commits,)


async def test_remote_materialization_replaces_stale_local_files(tmp_path: Path, monkeypatch) -> None:
    # Given remote storage and a local tree holding a stale copy plus a file storage does not have.
    agent_id = uuid.uuid4()
    storage = RemoteStorage({f"{agent_id}/soul.md": b"remote soul", f"{agent_id}/memory/notes.md": b"remote notes"})
    monkeypatch.setattr(module, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    agent_dir = tmp_path / str(agent_id)
    (agent_dir / "memory").mkdir(parents=True)
    (agent_dir / "soul.md").write_bytes(b"stale local soul")
    (agent_dir / "scratch.txt").write_bytes(b"local only")
    # When the engine rehydrates the directory.
    _ = await AgentManager.__new__(AgentManager)._materialize_agent_dir(agent_id)
    # Then remote content wins for the keys it holds and nothing else is disturbed.
    assert (agent_dir / "soul.md").read_bytes() == b"remote soul"
    assert (agent_dir / "memory" / "notes.md").read_bytes() == b"remote notes"
    assert (agent_dir / "scratch.txt").read_bytes() == b"local only"


type MirrorTree = tuple[AgentManager, dict[str, bytes], uuid.UUID]


@pytest.fixture
def mirror_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MirrorTree:
    """A mirror-active manager: only the durable store may be read, so the facade's merged view is a bare namespace."""
    objects: dict[str, bytes] = {}
    monkeypatch.setattr(module, "get_storage_backend", lambda: SimpleNamespace())
    monkeypatch.setattr(agent_materialize, "durable_store", lambda: RemoteStorage(objects))
    monkeypatch.setattr(module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    return AgentManager.__new__(AgentManager), objects, uuid.uuid4()


def _files(agent_dir: Path) -> list[str]:
    return sorted(
        path.relative_to(agent_dir).as_posix()
        for path in agent_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


async def test_mirror_materialization_writes_only_what_the_mirror_does_not_own(
    mirror_tree: MirrorTree, tmp_path: Path
) -> None:
    # Given a durable store holding mirrored keys, runtime state, the mirror marker and keys that cannot be local paths.
    manager, objects, agent_id = mirror_tree
    owned = [
        "workspace/notes.md",
        "memory/m.md",
        "skills/x/SKILL.md",
        "soul.md",
        ".openclaw/agents/a.json",
        ".maraclaw-mirror-generation",
    ]
    kept = ["openclaw.json", "agents/main/agent/auth.json", ".cache/blob"]
    unsafe = ["a/../../escape", "../outside.txt"]
    objects.update({f"{agent_id}/{rel}": rel.encode() for rel in [*owned, *kept, *unsafe]})
    # When the agent directory is rehydrated.
    agent_dir = await manager._materialize_agent_dir(agent_id)
    # Then only the keys nothing else owns are written, under the agent directory, and the layout still exists.
    assert _files(agent_dir) == sorted(kept)
    assert {rel: (agent_dir / rel).read_bytes() for rel in kept} == {rel: rel.encode() for rel in kept}
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "outside.txt").exists()
    assert (agent_dir / "workspace").is_dir()
    assert (agent_dir / ".openclaw" / "workspace").is_symlink()


async def test_mirror_materialization_leaves_files_a_state_snapshot_restored(mirror_tree: MirrorTree) -> None:
    # Given legacy keys in the durable store for files a state snapshot restore just placed, and one it did not.
    manager, objects, agent_id = mirror_tree
    restored = frozenset({DATABASE, ".openclaw-gateway-token"})
    objects.update({f"{agent_id}/{rel}": b"legacy" for rel in [*restored, "openclaw.json"]})
    agent_dir = manager._agent_dir(agent_id)
    for rel in restored:
        (agent_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        _ = (agent_dir / rel).write_bytes(b"restored")
    # When the agent directory is rehydrated.
    _ = await manager._materialize_agent_dir(agent_id, restored=restored)
    # Then the restored files keep their content and the rest still hydrates.
    assert {rel: (agent_dir / rel).read_bytes() for rel in restored} == dict.fromkeys(restored, b"restored")
    assert (agent_dir / "openclaw.json").read_bytes() == b"legacy"


@pytest.mark.parametrize("planted", ["parent", "destination"])
async def test_mirror_materialization_never_writes_through_a_guest_symlink(
    mirror_tree: MirrorTree, tmp_path: Path, planted: str
) -> None:
    # Given a guest-planted symlink on the path of one stored file, pointing outside the agent directory.
    manager, objects, agent_id = mirror_tree
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _ = (elsewhere / "keep.txt").write_text("keep")
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    if planted == "parent":
        (agent_dir / "agents").symlink_to(elsewhere)
    else:
        (agent_dir / "openclaw.json").symlink_to(elsewhere / "keep.txt")
    objects.update(
        {
            f"{agent_id}/agents/main/auth.json": b"pwned",
            f"{agent_id}/openclaw.json": b"pwned",
            f"{agent_id}/.cache/blob": b"fine",
        }
    )
    # When the agent directory is rehydrated.
    _ = await manager._materialize_agent_dir(agent_id)
    # Then nothing was written outside it, and the other files still hydrate.
    assert sorted(entry.name for entry in elsewhere.iterdir()) == ["keep.txt"]
    assert (elsewhere / "keep.txt").read_text() == "keep"
    assert (agent_dir / ".cache" / "blob").read_bytes() == b"fine"


async def test_facade_materialization_never_writes_through_a_directory_swapped_for_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a stored file whose directory becomes a symlink after the engine checked it and before it wrote.
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def swap() -> None:
        shutil.rmtree(agent_dir / "data")
        (agent_dir / "data").symlink_to(elsewhere)

    monkeypatch.setattr(module, "get_storage_backend", lambda: SwappingStorage({f"{agent_id}/data/x.txt": b"x"}, swap))
    monkeypatch.setattr(agent_materialize, "durable_store", lambda: None)
    monkeypatch.setattr(module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    # When the agent directory is rehydrated.
    _ = await AgentManager.__new__(AgentManager)._materialize_agent_dir(agent_id)
    # Then the write was refused rather than redirected.
    assert list(elsewhere.iterdir()) == []


async def test_with_the_mirror_off_every_key_is_materialized_from_the_facade_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given remote storage with the mirror off, whose flat listing must not be used.
    agent_id = uuid.uuid4()
    storage = RemoteStorage({f"{agent_id}/workspace/notes.md": b"notes", f"{agent_id}/.openclaw/state.json": b"state"})

    def forbidden(_prefix: str) -> AsyncGenerator[StorageEntry]:
        raise AssertionError("the flat listing belongs to the mirror")

    monkeypatch.setattr(storage, "list_files_flat", forbidden)
    monkeypatch.setattr(module, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_materialize, "durable_store", lambda: None)
    monkeypatch.setattr(module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    # When the agent directory is rehydrated.
    agent_dir = await AgentManager.__new__(AgentManager)._materialize_agent_dir(agent_id)
    # Then in-scope keys are written as before, exactly as without a mirror.
    assert (agent_dir / "workspace" / "notes.md").read_bytes() == b"notes"
    assert (agent_dir / ".openclaw" / "state.json").read_bytes() == b"state"
