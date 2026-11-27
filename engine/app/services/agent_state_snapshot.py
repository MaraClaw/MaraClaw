"""Rules every agent state snapshot path shares: key layout, host digest, snapshot flock, one-off container, stores.

A snapshot is one `openclaw backup create` archive of the OpenClaw state the workspace mirror does not cover, kept in
S3 under `state-snapshots/<agent id>/`, an unmetered quota passthrough prefix. Contract: docs/agent-state-snapshots.md.
"""

import hashlib
import re
import secrets
import stat
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, aclosing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from errno import ELOOP, ENOTDIR
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from pywhales import DockerClient
from pywhales.exceptions import DockerException, DockerTimeout

from app.config import get_settings
from app.services.agent_setup_lock import agent_flock
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.channel_files import lstat_guest_path
from app.services.workspace_mirror import durable_store, primary_store
from app.services.workspace_mirror_local import MARKER
from app.services.workspace_mirror_scope import WalkLimitError, in_scope, walk_local

PREFIX: Final = "state-snapshots"
GUEST_STATE_DIR: Final = "/home/node/custom-state"
CONTAINER_TMP: Final = "/tmp"  # noqa: S108 - a path inside the one-off container, never the host's temp directory
AGENT_DB: Final = "agents/main/agent/openclaw-agent.sqlite"
STATE_DBS: Final = (AGENT_DB, "state/openclaw.sqlite")
ARCHIVE_NAME: Final = "snapshot.tar.gz"
ROLE_LABEL: Final = "maraclaw.state-snapshot"
# Not the guest's `maraclaw.agent_id`: OOM events and guest scans read that label as the agent's guest.
AGENT_ID_LABEL: Final = "maraclaw.state-snapshot.agent_id"
ONE_OFF_MEMORY_BYTES: Final = 1024**3
ONE_OFF_CPUS: Final = 1.0
ONE_OFF_PIDS: Final = 128
# The command inside kills OpenClaw after 120 seconds; the CLI bound adds room for the container's start and exit.
ONE_OFF_TIMEOUT_SECONDS: Final = 300.0
WALK_MAX_ENTRIES: Final = 100_000
WALK_SECONDS: Final = 30.0
STORAGE_ERRORS: Final = (BotoCoreError, ClientError, OSError, RuntimeError, ValueError)

_WORK_DIR = "_state_snapshots"
_SKIPPED_ROOTS = frozenset({"workspace", "logs", "tmp", "plugin-skills"})
_SIDECARS = ("-wal", "-shm", "-journal")
_STAMP = "%Y%m%dT%H%M%S%f"
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_NAME = re.compile(rf"(\d{{8}}T\d{{12}})Z_([0-9a-f]{{16}})_(none|{_UUID})\.tar\.gz(\.held)?")


class SnapshotBusyError(RuntimeError):
    """Another snapshot, restore or prefix deletion of the agent holds its snapshot flock."""


class SnapshotDeleteError(RuntimeError):
    """Storage failed to delete the agent's snapshot prefix."""


@dataclass(frozen=True, slots=True, order=True)
class SnapshotName:
    """One snapshot object; names order by creation time."""

    stamp: str
    digest: str
    tenant: str

    @property
    def file_name(self) -> str:
        return f"{self.stamp}Z_{self.digest}_{self.tenant}.tar.gz"

    def key(self, agent_id: UUID) -> str:
        return agent_prefix(agent_id) + self.file_name

    def held_key(self, agent_id: UUID) -> str:
        return f"{self.key(agent_id)}.held"


@dataclass(frozen=True, slots=True)
class SnapshotListing:
    sizes: Mapping[SnapshotName, int]
    held: frozenset[SnapshotName]

    def newest(self) -> SnapshotName | None:
        return max(self.sizes, default=None)

    def restorable(self, tenant: str) -> SnapshotName | None:
        """The newest snapshot of `tenant` that carries no held marker."""
        return max((name for name in self.sizes if name.tenant == tenant and name not in self.held), default=None)

    def superseded(self, current: SnapshotName) -> list[SnapshotName]:
        """Snapshots older than `current` that a prune may delete: every one without a held marker."""
        return sorted(name for name in self.sizes if name < current and name not in self.held)


def agent_prefix(agent_id: UUID) -> str:
    return f"{PREFIX}/{agent_id}/"


def tenant_token(tenant_id: UUID | None) -> str:
    return "none" if tenant_id is None else str(tenant_id)


def new_snapshot_name(digest: str, tenant: str, now: datetime) -> SnapshotName:
    return SnapshotName(now.astimezone(UTC).strftime(_STAMP), digest, tenant)


def parse_name(file_name: str) -> tuple[SnapshotName, bool] | None:
    """The snapshot a key's last segment names and whether it is that snapshot's held marker; None for anything else."""
    match = _NAME.fullmatch(file_name)
    if match is None:
        return None
    stamp, digest, tenant, held = match.group(1, 2, 3, 4)
    try:
        _ = datetime.strptime(f"{stamp}+0000", "%Y%m%dT%H%M%S%f%z")
    except ValueError:
        return None
    return SnapshotName(stamp, digest, tenant), held is not None


async def list_snapshots(store: StorageBackend, agent_id: UUID) -> SnapshotListing:
    """One flat listing of the agent's prefix; keys that do not parse are ignored."""
    prefix = agent_prefix(agent_id)
    sizes: dict[SnapshotName, int] = {}
    held: set[SnapshotName] = set()
    async with aclosing(store.list_files_flat(prefix)) as entries:
        async for entry in entries:
            parsed = parse_name(entry.key.removeprefix(prefix))
            if parsed is None:
                continue
            name, marker = parsed
            if marker:
                held.add(name)
            else:
                sizes[name] = entry.size
    return SnapshotListing(sizes, frozenset(held))


def snapshot_store() -> StorageBackend | None:
    """The store snapshots are taken into and restored from; None unless snapshots are on and the mirror is active."""
    return durable_store() if get_settings().AGENT_STATE_SNAPSHOT_ENABLED else None


def _excluded(rel: str) -> bool:
    """The mirror's files, the trees OpenClaw regenerates or treats as volatile, and the engine's own bookkeeping."""
    head = rel.partition("/")[0]
    return head in _SKIPPED_ROOTS or head == MARKER or head.startswith(".maraclaw-rpc") or in_scope(rel)


def covered_directory(rel: str) -> bool:
    return rel not in _SKIPPED_ROOTS


def _digested(rel: str) -> bool:
    return not _excluded(rel) and not rel.casefold().endswith("-shm")


def placeable(rel: str) -> bool:
    """Whether a restore places the archived file at `rel`: covered by snapshots and not a SQLite sidecar."""
    return not _excluded(rel) and not rel.casefold().endswith(_SIDECARS)


def state_digest(agent_dir: Path, tenant: str) -> str:
    """16 hex digits over `tenant` and the path, size and mtime of every file snapshots cover; never follows a symlink.

    A tree past the walk's bounds gets a random digest, so it counts as changed on every pass instead of never.
    """
    try:
        files = walk_local(
            agent_dir,
            deadline_seconds=WALK_SECONDS,
            max_entries=WALK_MAX_ENTRIES,
            descend=covered_directory,
            include=_digested,
        )
    except WalkLimitError:
        return secrets.token_hex(8)
    digest = hashlib.sha256(tenant.encode())
    for file in files:
        digest.update(f"\0{file.rel}\0{file.size}\0{file.mtime_ns}".encode(errors="surrogateescape"))
    return digest.hexdigest()[:16]


def has_host_state(agent_dir: Path) -> bool:
    """Whether either OpenClaw database is a regular file in the agent directory, reached without following a symlink."""
    for rel in STATE_DBS:
        try:
            info = lstat_guest_path(agent_dir, rel)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in (ELOOP, ENOTDIR):
                continue
            raise
        if stat.S_ISREG(info.st_mode):
            return True
    return False


def snapshot_lock(agent_dir: Path) -> AbstractAsyncContextManager[None]:
    """The agent's snapshot flock beside its setup lock, taken without waiting; `SnapshotBusyError` while it is held.

    Lock order is the exclusive setup lock first, then this flock, everywhere.
    """
    busy = SnapshotBusyError("A state snapshot of this agent is being taken, restored or deleted")
    return agent_flock(agent_dir, "snapshot", wait_seconds=0, busy=busy)


async def delete_agent_snapshots(agent_dir: Path, agent_id: UUID) -> None:
    """Delete the agent's snapshots and held markers from the S3 primary, whatever the snapshot and mirror settings.

    The caller holds the exclusive setup lock. `SnapshotBusyError` while a snapshot or restore of the agent runs and
    `SnapshotDeleteError` when storage fails both leave the caller's job pending.
    """
    store = primary_store()
    if store is None:
        return
    async with snapshot_lock(agent_dir):
        try:
            await store.delete_tree(agent_prefix(agent_id))
        except STORAGE_ERRORS as exc:
            raise SnapshotDeleteError(f"Deleting the state snapshots failed: {type(exc).__name__}") from exc


def work_root(agents_root: Path) -> Path:
    return agents_root / _WORK_DIR


def new_work_dir(agents_root: Path, *parts: str) -> Path:
    """A fresh private directory below the agents root, the only host tree Docker is handed for snapshot work."""
    directory = work_root(agents_root).joinpath(*parts, uuid4().hex)
    directory.mkdir(mode=0o700, parents=True)
    return directory


def run_one_off(
    client: DockerClient,
    image: str,
    script: str,
    *,
    name: str,
    role: str,
    agent_id: UUID,
    mounts: Mapping[Path, str],
    envs: Mapping[str, str],
) -> str:
    """Run `script` under sh in a removed, network-less one-off container of `image` and return its stdout.

    The image's entrypoint (its validators and the guest bootstrap) does not run. Every mount is a bind whose source
    must exist: Docker refuses a missing one instead of creating it. A CLI timeout force-removes the container, which
    the CLI itself leaves running.
    """
    try:
        return client.run(
            image,
            ["-c", script],
            entrypoint="sh",
            envs=envs,
            init=True,
            labels={ROLE_LABEL: role, AGENT_ID_LABEL: str(agent_id)},
            memory=ONE_OFF_MEMORY_BYTES,
            memory_swap=ONE_OFF_MEMORY_BYTES,
            cpus=ONE_OFF_CPUS,
            pids_limit=ONE_OFF_PIDS,
            mounts=[["type=bind", f"source={source}", f"target={target}"] for source, target in mounts.items()],
            name=name,
            networks=["none"],
            pull="never",
            remove=True,
            workdir=CONTAINER_TMP,
            timeout=ONE_OFF_TIMEOUT_SECONDS,
        )
    except DockerTimeout:
        with suppress(DockerException):
            client.container.remove(name, force=True)
        raise
