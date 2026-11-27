"""Contained mounted-guest reads and atomic publication of verified channel files."""

import hashlib
import os
import shutil
import stat
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final
from uuid import uuid4

from app.records.channel_media import StoredFile
from app.services.openclaw_transcript import _open_contained
from app.services.storage_runtime.base import StorageReadLimitExceededError

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_COPY_CHUNK_BYTES: Final = 1024 * 1024


class GuestFileChangedError(OSError):
    """A guest file moved between two observations of it."""


@dataclass(frozen=True, slots=True)
class GuestStamp:
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int
    mode: int

    @classmethod
    def of(cls, info: os.stat_result) -> GuestStamp:
        return cls(info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink, info.st_mode)

    def matches(self, other: GuestStamp) -> bool:
        """Same inode, size and timestamps; the link count and mode may move without the contents doing so."""
        return (self.ino, self.size, self.mtime_ns, self.ctime_ns) == (
            other.ino,
            other.size,
            other.mtime_ns,
            other.ctime_ns,
        )


def _relative(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Not a relative path inside the agent directory: {relative!r}")
    return path


def _enter_parent(stack: ExitStack, root: Path, path: PurePosixPath, *, create: bool) -> int:
    """Open the directory that holds `path`, one O_NOFOLLOW component at a time; the stack closes every descriptor."""
    directory = os.open(root, _DIRECTORY)
    stack.callback(os.close, directory)
    for part in path.parts[:-1]:
        if create:
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o755, dir_fd=directory)
        directory = os.open(part, _DIRECTORY, dir_fd=directory)
        stack.callback(os.close, directory)
    return directory


def _read_bounded(fd: int, limit: int) -> bytes:
    with os.fdopen(os.dup(fd), "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise StorageReadLimitExceededError(limit, len(data))
    return data


def read_guest_file(root: Path, relative: str, limit: int) -> bytes:
    with _open_contained(root, PurePosixPath(relative)) as fd:
        return _read_bounded(fd, limit)


def stat_guest_file(root: Path, relative: str) -> GuestStamp:
    with _open_contained(root, _relative(relative)) as fd:
        return GuestStamp.of(os.fstat(fd))


def lstat_guest_path(root: Path, relative: str) -> os.stat_result:
    """Stat whatever sits at a guest path without following it or any parent; `FileNotFoundError` when nothing does.

    A symlink or file in place of a parent directory raises `OSError` (ELOOP or ENOTDIR), never a missing file.
    """
    path = _relative(relative)
    with ExitStack() as stack:
        directory = _enter_parent(stack, root, path, create=False)
        return os.stat(path.name, dir_fd=directory, follow_symlinks=False)


def read_guest_file_stamped(root: Path, relative: str, limit: int) -> tuple[bytes, GuestStamp]:
    """Bounded read that raises `GuestFileChangedError` when the file moved while it was being read."""
    with _open_contained(root, _relative(relative)) as fd:
        before = GuestStamp.of(os.fstat(fd))
        data = _read_bounded(fd, limit)
        after = GuestStamp.of(os.fstat(fd))
    if not before.matches(after):
        raise GuestFileChangedError(f"Guest file changed while it was being read: {relative}")
    return data, after


@contextmanager
def open_guest_file(root: Path, relative: str) -> Generator[tuple[int, GuestStamp]]:
    """Yield a contained descriptor of a regular guest file and its stamp, for streaming."""
    with _open_contained(root, _relative(relative)) as fd:
        yield fd, GuestStamp.of(os.fstat(fd))


def publish_guest_file(root: Path, file: StoredFile, data: bytes) -> None:
    """Publish through directory descriptors, never following guest-controlled symlinks."""
    with ExitStack() as stack:
        relative = PurePosixPath(file.path)
        directory = _enter_parent(stack, root, relative, create=True)
        temporary = f".channel-{uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Refuse symlink destinations even though replace itself would not follow one.
            try:
                current = read_guest_file(root, file.path, file.size)
            except FileNotFoundError:
                current = None
            if current is not None and hashlib.sha256(current).hexdigest() == file.sha256 and len(current) == file.size:
                return
            os.replace(temporary, relative.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)


def write_guest_file(
    root: Path,
    relative: str,
    data: bytes | BinaryIO,
    *,
    expected: GuestStamp | None = None,
    require_absent: bool = False,
    mode: int | None = None,
) -> GuestStamp:
    """Atomically create or replace a guest file and return the stamp of what was written.

    `data` is bytes or a readable stream, which is copied in bounded chunks from its current position and left open.
    Missing parent directories are created. A symlink or directory on the path is refused without being touched.
    An existing destination keeps its permission bits; a new file gets exactly `mode` when one is given (set on the
    descriptor, so the umask does not mask it), else 0o644 less the umask. With `expected`, the destination must
    still be that file;
    with `require_absent`, nothing may sit there yet. Either check runs once the data is on disk, immediately
    before the replace, and raises `GuestFileChangedError` without replacing anything.
    """
    path = _relative(relative)
    with ExitStack() as stack:
        directory = _enter_parent(stack, root, path, create=True)
        temporary = f".mirror-{uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                if isinstance(data, bytes):
                    _ = stream.write(data)
                else:
                    shutil.copyfileobj(data, stream, _COPY_CHUNK_BYTES)
                stream.flush()
                os.fsync(stream.fileno())
                try:
                    current: os.stat_result | None = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current is not None and not stat.S_ISREG(current.st_mode):
                    raise OSError(f"Refusing to replace a non-file guest path: {relative}")
                if expected is not None and (current is None or not GuestStamp.of(current).matches(expected)):
                    raise GuestFileChangedError(f"Guest file changed before it was replaced: {relative}")
                if require_absent and current is not None:
                    raise GuestFileChangedError(f"Guest file appeared before it was created: {relative}")
                if current is not None:
                    os.fchmod(stream.fileno(), stat.S_IMODE(current.st_mode))
                elif mode is not None:
                    os.fchmod(stream.fileno(), mode)
                os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
                return GuestStamp.of(os.fstat(stream.fileno()))
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)


def delete_guest_file(root: Path, relative: str) -> None:
    with ExitStack() as stack:
        path = PurePosixPath(relative)
        directory = _enter_parent(stack, root, path, create=False)
        os.unlink(path.name, dir_fd=directory)


def delete_guest_file_if_unchanged(root: Path, relative: str, expected: GuestStamp) -> bool:
    """Delete the guest file only while it is still the regular file `expected` describes.

    Returns False when it is missing, no longer a regular file, or changed. The stamp check and the unlink are two
    steps: a rewrite between them is not detected.
    """
    path = _relative(relative)
    try:
        with ExitStack() as stack:
            directory = _enter_parent(stack, root, path, create=False)
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or not GuestStamp.of(current).matches(expected):
                return False
            os.unlink(path.name, dir_fd=directory)
            return True
    except FileNotFoundError:
        return False


def ensure_guest_directory(root: Path, relative: str) -> None:
    """Create a guest directory and its parents one O_NOFOLLOW component at a time.

    A symlink or file anywhere on the path, the last component included, is refused with `OSError` (ELOOP or ENOTDIR).
    """
    path = _relative(relative)
    with ExitStack() as stack:
        # `_enter_parent` opens every component but the last, so a placeholder name makes it open them all.
        _ = _enter_parent(stack, root, path / "_", create=True)


def copy_into_guest_file(root: Path, relative: str, source: Path, *, chunk_bytes: int = 1024 * 1024) -> None:
    """Stream `source` over a guest file in place, so an existing file keeps its inode, owner and mode.

    Missing parents are created one O_NOFOLLOW component at a time and the destination is opened with O_NOFOLLOW, so a
    symlink anywhere on the path is refused with `OSError` (ELOOP or ENOTDIR) and nothing outside `root` is written.
    A destination that is not a regular file is refused untouched, and an unreadable `source` leaves it unchanged.
    """
    path = _relative(relative)
    with source.open("rb") as reader, ExitStack() as stack:
        directory = _enter_parent(stack, root, path, create=True)
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"Refusing to write to a non-file guest path: {relative}")
            os.ftruncate(fd, 0)
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "wb") as writer:
            while chunk := reader.read(chunk_bytes):
                _ = writer.write(chunk)
