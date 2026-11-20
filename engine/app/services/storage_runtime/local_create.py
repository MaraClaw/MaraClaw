"""Local storage writes and deletes that never follow a symlink below the storage root (see `local_contained`)."""

import errno
import os
import shutil
import stat
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from app.services.storage_runtime.base import StorageLinkRefusedError
from app.services.storage_runtime.local_contained import contained_parent, refuse_if_link


def _refuse_link_at(directory: int, name: str) -> None:
    with suppress(FileNotFoundError):
        if stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            raise StorageLinkRefusedError


def publish_complete_file(root: Path, relative: str, data: bytes) -> os.stat_result | None:
    with contained_parent(root, relative, create=True) as (directory, name):
        temporary = f".storage-create-{uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                info = os.fstat(stream.fileno())
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                _refuse_link_at(directory, name)
                return None
            os.fsync(directory)
            return info
        finally:
            os.unlink(temporary, dir_fd=directory)


def write_contained_file(root: Path, relative: str, data: bytes) -> None:
    """Rewrite `relative` in place, so an existing file keeps its inode and mode; missing parents are created.

    A directory or other non-file at `relative` raises `OSError` and is left untouched.
    """
    with contained_parent(root, relative, create=True) as (directory, name):
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=directory)
        except OSError as error:
            refuse_if_link(directory, name, error)
            raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"Refusing to write to a non-file storage path: {relative}")
        os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "wb") as stream:
        _ = stream.write(data)


def delete_contained_entry(root: Path, relative: str) -> None:
    """Remove the file or directory tree at `relative`; `FileNotFoundError` when nothing is there."""
    with contained_parent(root, relative) as (directory, name):
        if name == ".":
            raise FileNotFoundError(relative)  # the storage root itself is never an entry to delete
        mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
        if stat.S_ISLNK(mode):
            raise StorageLinkRefusedError
        if stat.S_ISDIR(mode):
            shutil.rmtree(name, dir_fd=directory)
        else:
            os.unlink(name, dir_fd=directory)


def remove_empty_contained_directory(root: Path, relative: str) -> None:
    """`rmdir` the directory at `relative` while it is empty; a missing, non-empty or plain-file entry stays."""
    with suppress(FileNotFoundError, NotADirectoryError), contained_parent(root, relative) as (directory, name):
        if name == ".":
            return  # the storage root itself is never removed
        mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
        if stat.S_ISLNK(mode):
            raise StorageLinkRefusedError
        if not stat.S_ISDIR(mode):
            return
        try:
            os.rmdir(name, dir_fd=directory)
        except OSError as error:
            if error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise


def delete_contained_tree(root: Path, relative: str) -> None:
    """Remove the directory tree at `relative`; nothing there (or the root itself) is a no-op."""
    with suppress(FileNotFoundError), contained_parent(root, relative) as (directory, name):
        if name == ".":
            return
        if stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            raise StorageLinkRefusedError
        shutil.rmtree(name, dir_fd=directory)
