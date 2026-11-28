"""Fakes for the agent state snapshot tests: in-memory S3, OpenClaw-shaped archives and a scripted Docker CLI."""

import builtins
import fcntl
import io
import json
import sqlite3
import tarfile
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

from pywhales.exceptions import DockerException

from app.services.agent_state_snapshot import AGENT_DB, ARCHIVE_NAME, GUEST_STATE_DIR
from app.services.agent_state_snapshot_archive import ASSET_PAYLOAD
from app.services.storage_runtime.base import ConditionalWriteResult, StorageBackend, StorageEntry, WriteCondition
from app.services.storage_runtime.upload_source import UploadSource

ARCHIVE_ROOT = "2026-11-27T14-44-39.138+00-00-openclaw-backup"
IMAGE_VERSION = "2026.11.10"
STATE_ASSET = {"kind": "state", "sourcePath": GUEST_STATE_DIR, "archivePath": f"{ARCHIVE_ROOT}/{ASSET_PAYLOAD}"}


class MemoryStore(StorageBackend):
    """Flat object storage listed in key order; conditional creates are honoured and writes and deletes recorded.

    `failures` maps an operation (`list`, `read`, `write`, `delete`) to the error it raises.
    """

    def __init__(self, objects: Mapping[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.writes: list[tuple[str, WriteCondition | None, str | None]] = []
        self.deleted: list[str] = []
        self.listed: list[str] = []
        self.failures: dict[str, Exception] = {}

    def _fail(self, operation: str) -> None:
        if operation in self.failures:
            raise self.failures[operation]

    @override
    async def list_files_flat(self, prefix: str) -> AsyncGenerator[StorageEntry]:
        self.listed.append(prefix)
        self._fail("list")
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.objects[key]))

    @override
    async def write_source_if_match(
        self,
        key: str,
        source: UploadSource,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        self._fail("write")
        self.writes.append((key, condition, content_type))
        if condition is not None and condition.require_absent and key in self.objects:
            return ConditionalWriteResult(ok=False, conflict=True)
        self.objects[key] = source.body().read()
        return ConditionalWriteResult(ok=True)

    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        self._fail("write")
        self.writes.append((key, WriteCondition(require_absent=True), content_type))
        if key in self.objects:
            return ConditionalWriteResult(ok=False, conflict=True)
        self.objects[key] = data
        return ConditionalWriteResult(ok=True)

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        self._fail("read")
        if key not in self.objects:
            raise FileNotFoundError(key)
        start = offset or 0
        return self.objects[key][start : start + max_bytes]

    @override
    async def delete(self, key: str) -> None:
        self._fail("delete")
        self.deleted.append(key)
        _ = self.objects.pop(key, None)

    @override
    async def delete_tree(self, key: str) -> None:
        self._fail("delete")
        for existing in [existing for existing in self.objects if existing.startswith(key)]:
            await self.delete(existing)


def write_file(path: Path, data: bytes, *, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_bytes(data)
    path.chmod(mode)
    return path


def sqlite_file(path: Path, *rows: str) -> Path:
    """A closed rollback-journal database holding `rows`, so no sidecar is left beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        _ = db.execute("CREATE TABLE IF NOT EXISTS facts (value TEXT NOT NULL)")
        _ = db.executemany("INSERT INTO facts VALUES (?)", [(row,) for row in rows])
        db.commit()
    return path


def agent_database(
    path: Path,
    *,
    entries: Sequence[Mapping[str, object]] = ({"status": "done"},),
    pending: int = 0,
    inputs_table: bool = True,
) -> Path:
    """The OpenClaw agent database's session tables: one `session_nodes` row per entry, `pending` queued inputs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        _ = db.execute("CREATE TABLE session_nodes (session_key TEXT PRIMARY KEY, entry_json TEXT NOT NULL) STRICT")
        _ = db.executemany(
            "INSERT INTO session_nodes VALUES (?, ?)",
            [(f"agent:main:{number}", json.dumps(entry)) for number, entry in enumerate(entries)],
        )
        if inputs_table:
            _ = db.execute("CREATE TABLE session_pending_inputs (id INTEGER PRIMARY KEY, state TEXT NOT NULL)")
            _ = db.executemany("INSERT INTO session_pending_inputs (state) VALUES (?)", [("queued",)] * pending)
        db.commit()
    return path


def openclaw_state(agent_dir: Path) -> Path:
    """An idle agent directory as OpenClaw leaves it, with workspace and mirror-scope files beside its state."""
    _ = agent_database(agent_dir / AGENT_DB)
    _ = sqlite_file(agent_dir / "state" / "openclaw.sqlite", "state")
    _ = sqlite_file(agent_dir / "knowledge" / "registry.sqlite", "registry")
    _ = sqlite_file(agent_dir / "knowledge" / "stores" / "0b6b3c1e" / "state.sqlite", "claim")
    _ = write_file(agent_dir / ".openclaw-gateway-token", b"gateway-token", mode=0o600)
    _ = write_file(agent_dir / "agents" / "main" / "agent" / "auth-profiles.json", b"{}", mode=0o600)
    _ = write_file(agent_dir / "workspace" / "AGENTS.md", b"workspace")
    _ = write_file(agent_dir / "memory" / "notes.md", b"mirrored")
    return agent_dir


def write_archive(
    archive: Path,
    state: Path,
    *,
    runtime_version: str = IMAGE_VERSION,
    assets: Sequence[Mapping[str, object]] = (STATE_ASSET,),
) -> Path:
    """Pack `state` as `openclaw backup create` lays out its archive: a manifest and one state asset's payload."""
    manifest = json.dumps({"schemaVersion": 1, "runtimeVersion": runtime_version, "assets": list(assets)}).encode()
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        tar.add(state, arcname=f"{ARCHIVE_ROOT}/{ASSET_PAYLOAD}")
    return archive


def backup_report(**changes: object) -> str:
    """The JSON `openclaw backup create --json` prints, with `changes` applied."""
    report: dict[str, object] = {
        "createdAt": "2026-11-27T14:44:39.138Z",
        "archiveRoot": ARCHIVE_ROOT,
        "archivePath": f"/out/{ARCHIVE_NAME}",
        "dryRun": False,
        "includeWorkspace": False,
        "onlyConfig": False,
        "verified": True,
        "assets": [{**STATE_ASSET, "displayPath": "~/custom-state"}],
        "skipped": [],
        "warnings": [],
    }
    return json.dumps(report | changes)


def flock_free(path: Path) -> bool:
    """Whether an exclusive flock on `path` could be taken now (it is released at once)."""
    with path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(lock, fcntl.LOCK_UN)
        return True


@dataclass(frozen=True, slots=True)
class DockerRun:
    image: str
    command: list[str]
    options: dict[str, Any]
    mounts: dict[str, Path]


@dataclass(frozen=True, slots=True)
class Listed:
    id: str


@dataclass
class Containers:
    """`docker container ls/rm` over the containers named in `labelled` (label filter -> container ids)."""

    labelled: dict[str, builtins.list[str]] = field(default_factory=dict)
    filters: builtins.list[object] = field(default_factory=builtins.list)
    removed: builtins.list[tuple[str, bool]] = field(default_factory=builtins.list)

    def list(self, *, filters: Sequence[tuple[str, str]] = (), **_options: object) -> builtins.list[Listed]:
        self.filters.append(filters)
        return [Listed(container) for _key, value in filters for container in self.labelled.get(value, [])]

    def remove(self, container: str, *, force: bool = False) -> None:
        self.removed.append((container, force))


type Act = Callable[[dict[str, Path]], str]


@dataclass
class Clock:
    """A monotonic clock a test steps by hand."""

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedDocker:
    """A Docker CLI whose `run` hands its bind mounts (target -> host source) to `act` and returns what it prints.

    A missing bind source fails the run as `--mount type=bind` does, instead of being created.
    """

    def __init__(self, act: Act) -> None:
        self.act: Act = act
        self.runs: list[DockerRun] = []
        self.container: Containers = Containers()

    def run(self, image: str, command: list[str], **options: Any) -> str:
        mounts: dict[str, Path] = {}
        for parts in options["mounts"]:
            fields = dict(part.split("=", 1) for part in parts)
            assert fields["type"] == "bind"
            source = Path(fields["source"])
            if not source.exists():
                raise DockerException(["docker", "run"], 125, b"", b"bind source path does not exist")
            mounts[fields["target"]] = source
        self.runs.append(DockerRun(image, command, options, mounts))
        return self.act(mounts)


def backup_of(report: str | None = None) -> Act:
    """`openclaw backup create`: archive the mounted state directory into /out and print `report`."""

    def act(mounts: dict[str, Path]) -> str:
        _ = write_archive(mounts["/out"] / ARCHIVE_NAME, mounts[GUEST_STATE_DIR])
        return backup_report() if report is None else report

    return act


def crashed(*, exit_code: int = 1) -> Act:
    """A one-off container that exits non-zero without leaving anything."""

    def act(_mounts: dict[str, Path]) -> str:
        raise DockerException(["docker", "run"], exit_code, b"", b"")

    return act


def restore_by(*, image_version: str = IMAGE_VERSION, exit_code: int = 0) -> Act:
    """The restore script: record the image's version, then restore the archive into /work/stage unless it fails."""

    def act(mounts: dict[str, Path]) -> str:
        work = mounts["/work"]
        _ = (work / "version.txt").write_text(f"OpenClaw {image_version} (6ea542f)\n")
        if exit_code == 0:
            with tarfile.open(work / "in" / ARCHIVE_NAME, "r:gz") as tar:
                tar.extractall(work / "stage", filter="data")
            result = {"ok": True, "archiveRoot": ARCHIVE_ROOT, "symlinkCount": 0, "targetPath": "/work/stage"}
            _ = (work / "result.json").write_text(json.dumps(result))
        _ = (work / "exit_code").write_text(f"{exit_code}\n")
        return ""

    return act
