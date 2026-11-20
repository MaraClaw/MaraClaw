"""Local storage reads that never follow a symlink below the storage root.

Every path is opened one component at a time from the resolved root, each with O_NOFOLLOW, and the work is done on
the descriptors held. A link anywhere on the path, planted before the call or swapped in during it, raises
`StorageLinkRefusedError` (403) instead of being followed. Missing paths keep their usual answers.
"""

import errno
import os
import stat
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, suppress
from io import FileIO
from pathlib import Path, PurePosixPath

from app.services.storage_runtime.base import (
    StorageEntry,
    StorageLinkRefusedError,
    StoragePage,
    StorageReadLimitExceededError,
    StorageVersion,
    content_hash_bytes,
)
from app.services.storage_runtime.bounded import read_bounded

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ENTRY = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def refuse_if_link(directory: int, name: str, error: OSError) -> None:
    """Raise 403 when `error` came from a link at `name`; otherwise return so the caller re-raises `error`.

    O_NOFOLLOW reports a link as ELOOP, or as ENOTDIR together with O_DIRECTORY (Linux), which a plain file in the
    way also reports, so ENOTDIR looks at the entry itself.
    """
    if error.errno == errno.ELOOP:
        raise StorageLinkRefusedError from error
    if error.errno == errno.ENOTDIR:
        with suppress(OSError):
            if stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
                raise StorageLinkRefusedError from error


def open_directory(directory: int, name: str, *, create: bool = False) -> int:
    """Open the directory `name` inside `directory` without following a link, creating it first when asked."""
    if create:
        with suppress(FileExistsError):
            os.mkdir(name, mode=0o755, dir_fd=directory)
    try:
        return os.open(name, _DIRECTORY, dir_fd=directory)
    except OSError as error:
        refuse_if_link(directory, name, error)
        raise


@contextmanager
def contained_parent(root: Path, relative: str, *, create: bool = False) -> Generator[tuple[int, str]]:
    """Yield a descriptor of the directory holding `relative` and its last name (`"."` for the root itself).

    `root` must be resolved. With `create`, the root and every missing parent are made on the way down.
    """
    path = PurePosixPath(relative)
    with ExitStack() as stack:
        if create:
            root.mkdir(parents=True, exist_ok=True)
        directory = os.open(root, _DIRECTORY)
        stack.callback(os.close, directory)
        for part in path.parts[:-1]:
            directory = open_directory(directory, part, create=create)
            stack.callback(os.close, directory)
        yield directory, path.name or "."


@contextmanager
def open_contained(root: Path, relative: str) -> Generator[tuple[int, os.stat_result] | None]:
    """Yield a descriptor of whatever sits at `relative` (a file or a directory) with its `fstat`, or None if nothing."""
    fd: int | None = None
    with suppress(FileNotFoundError, NotADirectoryError), contained_parent(root, relative) as (directory, name):
        try:
            fd = os.open(name, _ENTRY, dir_fd=directory)
        except OSError as error:
            refuse_if_link(directory, name, error)
            raise
    if fd is None:
        yield None
        return
    try:
        yield fd, os.fstat(fd)
    finally:
        os.close(fd)


def lstat_entry(root: Path, relative: str) -> os.stat_result | None:
    """What sits at `relative`, without following it; None when nothing does."""
    info: os.stat_result | None = None
    with suppress(FileNotFoundError, NotADirectoryError), contained_parent(root, relative) as (directory, name):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise StorageLinkRefusedError
    return info


@contextmanager
def open_regular_entry(root: Path, relative: str) -> Generator[tuple[FileIO, int, str]]:
    """Yield a read stream of the regular file at `relative` with the descriptor of its directory and its name.

    Nothing above `root` is opened, so only the root and the folders below it need to be readable. A link anywhere on
    the path raises `StorageLinkRefusedError`. A missing path, a file where a directory should be, and an entry that is
    not a regular file (a directory, a pipe) raise `FileNotFoundError`; the entry is opened non-blocking and
    checked with `fstat` before anything reads it, so a pipe the guest planted cannot stall a worker.
    """
    with ExitStack() as stack:
        try:
            directory, name = stack.enter_context(contained_parent(root, relative))
        except NotADirectoryError as error:
            raise FileNotFoundError(relative) from error
        try:
            fd = os.open(name, _ENTRY, dir_fd=directory)
        except OSError as error:
            refuse_if_link(directory, name, error)
            raise
        stack.callback(os.close, fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FileNotFoundError(relative)
        yield stack.enter_context(FileIO(fd, "rb", closefd=False)), directory, name


@contextmanager
def open_regular_file(root: Path, relative: str) -> Generator[FileIO]:
    """Yield a read stream of the regular file at `relative`; `open_regular_entry` says what it refuses."""
    with open_regular_entry(root, relative) as (stream, _directory, _name):
        yield stream


def local_version_token(info: os.stat_result, file_hash: str | None) -> str:
    return f"{info.st_mtime_ns}:{info.st_size}:{file_hash or ''}"


def _read_regular(fd: int, info: os.stat_result, relative: str, max_bytes: int | None) -> bytes:
    if not stat.S_ISREG(info.st_mode):
        raise FileNotFoundError(relative)
    if max_bytes is not None and info.st_size > max_bytes:
        raise StorageReadLimitExceededError(max_bytes, info.st_size)
    with FileIO(fd, "rb", closefd=False) as stream:
        return stream.readall() if max_bytes is None else read_bounded(stream, max_bytes)


def read_version(root: Path, relative: str, max_bytes: int | None) -> StorageVersion:
    """The version of `relative` with its content hash, all from one contained descriptor."""
    with open_contained(root, relative) as opened:
        if opened is None:
            return StorageVersion(key=relative, exists=False, is_dir=False)
        fd, info = opened
        if stat.S_ISDIR(info.st_mode):
            return StorageVersion(
                key=relative,
                exists=True,
                is_dir=True,
                modified_at=str(info.st_mtime),
                version_id=local_version_token(info, None),
            )
        data = _read_regular(fd, info, relative, max_bytes)
    digest = content_hash_bytes(data)
    return StorageVersion(
        key=relative,
        exists=True,
        is_dir=False,
        size=info.st_size,
        modified_at=str(info.st_mtime),
        etag=digest,
        version_id=local_version_token(info, digest),
        content_hash=digest,
    )


def read_entry(root: Path, relative: str) -> StorageEntry:
    """`stat` of `relative`, hashing a file from the same descriptor that was measured."""
    with open_contained(root, relative) as opened:
        if opened is None:
            raise FileNotFoundError(relative)
        fd, info = opened
        regular = stat.S_ISREG(info.st_mode)
        digest = content_hash_bytes(_read_regular(fd, info, relative, None)) if regular else ""
    return StorageEntry(
        name=PurePosixPath(relative).name or root.name,
        key=relative,
        is_dir=stat.S_ISDIR(info.st_mode),
        size=info.st_size if regular else 0,
        modified_at=str(info.st_mtime),
        version_id=local_version_token(info, digest),
        etag=digest,
        content_hash=digest,
    )


def _open_listing(root: Path, relative: str) -> int | None:
    fd: int | None = None
    with suppress(FileNotFoundError, NotADirectoryError), contained_parent(root, relative) as (directory, name):
        fd = open_directory(directory, name)
    return fd


def _listed_names(directory: int) -> list[tuple[str, bool]]:
    """Each child name with whether it is a directory; symlinks and `.gitkeep` are never listed."""
    with os.scandir(directory) as scanner:
        return [
            (entry.name, entry.is_dir(follow_symlinks=False))
            for entry in scanner
            if entry.name != ".gitkeep" and not entry.is_symlink()
        ]


def _storage_entry(directory: int, name: str, prefix: str) -> StorageEntry | None:
    """The listed child `name`, or None once it is gone or has become a link."""
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return None
    return StorageEntry(
        name=name,
        key=f"{prefix}/{name}" if prefix else name,
        is_dir=stat.S_ISDIR(info.st_mode),
        size=info.st_size if stat.S_ISREG(info.st_mode) else 0,
        modified_at=str(info.st_mtime),
        version_id=local_version_token(info, None),
    )


def scan_directory(root: Path, relative: str) -> list[StorageEntry]:
    directory = _open_listing(root, relative)
    if directory is None:
        return []
    try:
        names = _listed_names(directory)
        return [entry for name, _is_dir in names if (entry := _storage_entry(directory, name, relative))]
    finally:
        os.close(directory)


def scan_page(root: Path, relative: str, limit: int, after: str) -> StoragePage:
    """Order names by `walk_order` first and `stat` only the returned slice."""
    directory = _open_listing(root, relative)
    if directory is None:
        return StoragePage([], False)
    try:
        ordered = sorted((f"{name}/" if is_dir else name, name) for name, is_dir in _listed_names(directory))
        later = [name for order, name in ordered if order > after]
        entries = [entry for name in later[:limit] if (entry := _storage_entry(directory, name, relative))]
        return StoragePage(entries, len(later) > limit)
    finally:
        os.close(directory)
