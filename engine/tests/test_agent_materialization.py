"""Storage rehydration never rewrites the files it reads from, yet still hydrates remote storage."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from typing import override

import pytest

from app.services import agent_manager as module
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
