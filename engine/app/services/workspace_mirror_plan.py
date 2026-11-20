"""Pure decisions of the workspace mirror: what one path needs, given its manifest entry and both sides of it."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath

from app.records.workspace_mirror_entry import WorkspaceMirrorEntryRecord
from app.services.workspace_mirror_scope import (
    MAX_FILE_BYTES,
    SETTLE_SECONDS,
    LocalFile,
    is_root_file,
    scope_storage_prefixes,
)

SETTLE_NS = SETTLE_SECONDS * 1_000_000_000
TOO_LARGE_RETRY = timedelta(hours=1)
MASS_DELETE_MIN = 20
CONFLICTS_DIRECTORY = "workspace/conflicts"


@dataclass(frozen=True, slots=True)
class Remote:
    """One durable object as the flat listing, or a HEAD for a root file, reports it."""

    etag: str
    size: int
    modified_at: str


class Action(StrEnum):
    NOOP = "noop"
    PUSH_CREATE = "push_create"
    PUSH_UPDATE = "push_update"
    PULL = "pull"
    DELETE_REMOTE = "delete_remote"
    DELETE_LOCAL = "delete_local"
    DROP = "drop"
    COMPARE = "compare"


PUSHES = frozenset({Action.PUSH_CREATE, Action.PUSH_UPDATE})
STORE_WRITES = PUSHES | {Action.DELETE_REMOTE}


def scope_directories() -> tuple[str, ...]:
    """Every directory a scope prefix runs through; each must be a real directory before anything below it moves."""
    directories: dict[str, None] = {}
    for prefix in scope_storage_prefixes():
        parts = prefix.rstrip("/").split("/")
        for depth in range(1, len(parts) + 1):
            directories["/".join(parts[:depth])] = None
    return tuple(directories)


def local_matches(entry: WorkspaceMirrorEntryRecord, local: LocalFile) -> bool:
    recorded = (entry.local_ino, entry.local_size, entry.local_mtime_ns, entry.local_ctime_ns)
    return recorded == (local.ino, local.size, local.mtime_ns, local.ctime_ns)


def remote_matches(entry: WorkspaceMirrorEntryRecord, remote: Remote | None) -> bool:
    """A too-large entry that never reached storage stays unchanged while storage still holds nothing."""
    if remote is None:
        return entry.remote_etag is None
    return (entry.remote_etag, entry.remote_size) == (remote.etag, remote.size)


def _modified_ns(raw: str) -> int | None:
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1_000


def freeze_reason(
    local: LocalFile | None,
    remote: Remote | None,
    base: WorkspaceMirrorEntryRecord | None,
    *,
    now_ns: int,
    settle_local: bool,
) -> str | None:
    """Why a path must sit this pass out untouched, or None when both sides are stable enough to decide on.

    An object the manifest recorded is not settling however recent it is: it is the copy the mirror last wrote or saw.
    """
    if local is not None:
        if local.nlink > 1:
            return "hard-linked guest file"
        if settle_local and abs(now_ns - local.mtime_ns) < SETTLE_NS:
            return "guest file still settling"
    if remote is not None:
        if remote.size > MAX_FILE_BYTES:
            return "stored object larger than the mirror cap"
        modified = _modified_ns(remote.modified_at)
        recorded = base is not None and remote_matches(base, remote)
        if not recorded and modified is not None and abs(now_ns - modified) < SETTLE_NS:
            return "stored object still settling"
    return None


def _backing_off(entry: WorkspaceMirrorEntryRecord, local: LocalFile | None, now: datetime) -> bool:
    """A file storage refused as too large is offered again only once it changed or an hour went by."""
    return (
        entry.skip_reason == "too_large"
        and local is not None
        and local_matches(entry, local)
        and entry.synced_at is not None
        and now - entry.synced_at < TOO_LARGE_RETRY
    )


def _without_base(local: LocalFile | None, remote: Remote | None) -> Action:
    if local is not None and remote is not None:
        return Action.COMPARE
    if local is not None:
        return Action.PUSH_CREATE
    if remote is not None:
        return Action.PULL
    return Action.DROP


def _local_side(rel: str, local: LocalFile | None, remote: Remote | None) -> Action:
    if local is not None:
        return Action.PUSH_CREATE if remote is None else Action.PUSH_UPDATE
    if remote is None:
        return Action.DROP
    # The guest's root files are never deleted from storage; a missing one is restored.
    return Action.PULL if is_root_file(rel) else Action.DELETE_REMOTE


def _both_sides(local: LocalFile | None, remote: Remote | None) -> Action:
    if local is None and remote is None:
        return Action.DROP
    if local is None:
        return Action.PULL
    if remote is None:
        return Action.PUSH_CREATE
    return Action.COMPARE


def decide(
    rel: str,
    entry: WorkspaceMirrorEntryRecord | None,
    local: LocalFile | None,
    remote: Remote | None,
    *,
    now: datetime,
    retry_too_large: bool = False,
) -> Action:
    """The one action for a path; `entry` is its base, a manifest row of the current generation, or None."""
    if entry is None:
        return _without_base(local, remote)
    # A too-large entry never reached storage, so its local bytes count as unsynced.
    local_changed = local is None or not local_matches(entry, local) or entry.skip_reason is not None
    remote_changed = not remote_matches(entry, remote)
    if not remote_changed:
        action = _local_side(rel, local, remote) if local_changed else Action.NOOP
    elif not local_changed:
        action = Action.DELETE_LOCAL if remote is None else Action.PULL
    else:
        action = _both_sides(local, remote)
    if action in PUSHES and not retry_too_large and _backing_off(entry, local, now):
        return Action.NOOP
    return action


# A held-back delete becomes the copy the other way: a vanished guest tree is restored from storage and a vanished
# prefix (wiped, or an engine pointed at the wrong bucket) from the guest.
MASS_DELETE_RESTORES = {Action.DELETE_REMOTE: Action.PULL, Action.DELETE_LOCAL: Action.PUSH_CREATE}


def mass_delete(actions: dict[str, Action], base_count: int, delete: Action) -> bool:
    """Whether this pass would delete so much on one side that the other side more likely vanished than was pruned."""
    deletes = sum(1 for action in actions.values() if action is delete)
    return deletes >= MASS_DELETE_MIN and deletes * 2 >= base_count


def conflict_path(rel: str, now: datetime, attempt: int = 1) -> str:
    """Where the guest's losing bytes of `rel` are kept: under workspace/conflicts, stamped before the suffix."""
    path = PurePosixPath(rel)
    stamp = now.astimezone(UTC).strftime("%Y-%m-%d %H%M%S")
    tag = f" (conflict {stamp})" if attempt == 1 else f" (conflict {stamp} {attempt})"
    return f"{CONFLICTS_DIRECTORY}/{path.with_name(f'{path.stem}{tag}{path.suffix}')}"
