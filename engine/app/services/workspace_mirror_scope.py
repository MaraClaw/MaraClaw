"""Scope rules for the workspace mirror, and the contained walk of one local agent directory.

One predicate decides membership for both sides of the mirror: the durable listing and the local tree. The guest
shares the engine's uid and controls everything below the agent directory, so the walk only lists names through
directory descriptors and never opens, follows or reads anything it did not itself prove to be a directory.
"""

import os
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from app.records.organization_plan import OrganizationFileLimitBytes
from app.services.storage_runtime.utils import normalize_storage_key
from app.services.workspace_mirror_local import unsettled

ROOT_FILES = frozenset({"AGENTS.md", "HEARTBEAT.md", "soul.md", "state.json"})
SETTLE_SECONDS = 5
MAX_ENTRIES = 20_000
MAX_FILE_BYTES = int(max(OrganizationFileLimitBytes))

_TREE_ROOTS = ("workspace", "memory", "daily_reports", "skills")
_OUTBOUND = "media/outbound/"
_EXCLUDED_DIRECTORIES = frozenset({"node_modules", "__pycache__", "venv", "site-packages"})
_EXCLUDED_NAMES = (
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.sqlite-*",
    "*.db-*",
    "*-wal",
    "*-shm",
    "*-journal",
    "*.tmp",
    "*~",
    "*.swp",
    "*.part",
)
_GUEST_ROOT = "/home/node/custom-state/"
_MAX_DEPTH = 64
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class GuestPathError(ValueError):
    """A guest-supplied path cannot name a file inside the agent directory."""


class WalkLimitError(Exception):
    """The local walk hit its entry, depth or time bound; what it listed so far is not the whole tree."""


@dataclass(frozen=True, slots=True)
class LocalFile:
    rel: str
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int
    mode: int


def _clean(rel: str) -> bool:
    """Valid relative posix path whose every segment may be mirrored; says nothing about the tree it sits in."""
    if not rel:
        return False
    try:
        _ = rel.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if any(ch == "\\" or unicodedata.category(ch) == "Cc" for ch in rel):
        return False
    if normalize_storage_key(rel) != rel:
        return False
    return not any(not part or part.startswith(".") or part in _EXCLUDED_DIRECTORIES for part in rel.split("/"))


def _in_tree(parts: list[str]) -> bool:
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    return parts[0] in _TREE_ROOTS or (parts[0] == "media" and parts[1] == "outbound" and len(parts) > 2)


def in_scope(rel: str) -> bool:
    """Whether the file at relative path `rel` is mirrored."""
    if not _clean(rel):
        return False
    parts = rel.split("/")
    name = parts[-1].casefold()
    if any(fnmatchcase(name, pattern) for pattern in _EXCLUDED_NAMES):
        return False
    return _in_tree(parts)


def is_shareable(rel: str) -> bool:
    return in_scope(rel) and rel.startswith(("workspace/", _OUTBOUND))


def is_root_file(rel: str) -> bool:
    return rel in ROOT_FILES


def normalize_guest_path(raw: str) -> str:
    """Map a path as the guest names it to a relative posix path; scope is checked separately by callers."""
    path = raw.strip()
    if "\x00" in path:
        raise GuestPathError("Guest path contains a NUL byte")
    path = path.removeprefix(_GUEST_ROOT)
    if path.startswith("/"):
        raise GuestPathError("Guest path is outside the agent directory")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise GuestPathError("Guest path contains a '..' segment")
    if not parts:
        raise GuestPathError("Guest path is empty")
    if parts[:2] == [".openclaw", "workspace"]:
        parts = ["workspace", *parts[2:]]
    return "/".join(parts)


def scope_storage_prefixes() -> tuple[str, ...]:
    """Relative prefixes the mirror lists remotely; root files are handled separately by callers."""
    return (*(f"{name}/" for name in _TREE_ROOTS), _OUTBOUND)


def case_collisions(paths: Iterable[str]) -> set[str]:
    """Paths that differ from another path only by case, which a case-insensitive filesystem cannot hold apart."""
    groups: dict[str, set[str]] = {}
    for path in paths:
        groups.setdefault(path.casefold(), set()).add(path)
    return {path for group in groups.values() if len(group) > 1 for path in group}


def _directory_in_scope(rel: str) -> bool:
    """Whether the walk should descend into directory `rel`: some in-scope file may sit below it."""
    if not _clean(rel):
        return False
    parts = rel.split("/")
    return parts[0] in _TREE_ROOTS or (parts[0] == "media" and (len(parts) == 1 or parts[1] == "outbound"))


@dataclass(slots=True)
class _Level:
    rel: str
    fd: int
    pending: list[str] = field(default_factory=list)


def walk_local(
    root: Path,
    *,
    deadline_seconds: float,
    max_entries: int = MAX_ENTRIES,
    descend: Callable[[str], bool] = _directory_in_scope,
    include: Callable[[str], bool] = in_scope,
) -> list[LocalFile]:
    """List the regular files below `root` without following or opening anything the guest controls.

    By default the walk lists the mirror's scope; `descend` and `include` take the place of its directory and file
    predicates. Symlinks, FIFOs, sockets and devices are skipped without being opened, and directories `descend`
    rejects are never entered. Every directory entry examined counts toward `max_entries`. A missing `root` raises
    `FileNotFoundError`; a tree that is too large, too deep or too slow raises `WalkLimitError` because the
    partial listing would make unlisted files look deleted. An entry the guest moved, swapped or made unreadable
    meanwhile is left out, as the mirror proves every unlisted path absent before it reads one as deleted; a host
    fault raises `OSError`.
    """
    deadline = time.monotonic() + deadline_seconds
    files: list[LocalFile] = []
    scanned = 0

    def scan(level: _Level) -> list[str]:
        nonlocal scanned
        subdirectories: list[str] = []
        with os.scandir(level.fd) as entries:
            for entry in entries:
                scanned += 1
                if scanned > max_entries:
                    raise WalkLimitError(f"More than {max_entries} entries below the agent directory")
                if time.monotonic() >= deadline:
                    raise WalkLimitError("Walk deadline passed")
                rel = f"{level.rel}/{entry.name}" if level.rel else entry.name
                if entry.is_dir(follow_symlinks=False):
                    if descend(rel):
                        subdirectories.append(entry.name)
                elif include(rel):
                    try:
                        info = os.stat(entry.name, dir_fd=level.fd, follow_symlinks=False)
                    except OSError as exc:
                        if not unsettled(exc):
                            raise
                        continue
                    if stat.S_ISREG(info.st_mode):
                        files.append(
                            LocalFile(
                                rel,
                                info.st_ino,
                                info.st_size,
                                info.st_mtime_ns,
                                info.st_ctime_ns,
                                info.st_nlink,
                                stat.S_IMODE(info.st_mode),
                            )
                        )
        return subdirectories

    levels = [_Level("", os.open(root, _DIRECTORY))]
    try:
        levels[0].pending = scan(levels[0])
        while levels:
            level = levels[-1]
            if not level.pending:
                _ = levels.pop()
                os.close(level.fd)
                continue
            if len(levels) > _MAX_DEPTH:
                raise WalkLimitError(f"Directories nested deeper than {_MAX_DEPTH} levels")
            name = level.pending.pop()
            try:
                fd = os.open(name, _DIRECTORY, dir_fd=level.fd)
            except OSError as exc:
                if not unsettled(exc):
                    raise
                continue
            child = _Level(f"{level.rel}/{name}" if level.rel else name, fd)
            levels.append(child)
            child.pending = scan(child)
    finally:
        for level in levels:
            os.close(level.fd)
    return sorted(files, key=lambda file: file.rel)
