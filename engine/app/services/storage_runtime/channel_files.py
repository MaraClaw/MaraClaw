"""Contained mounted-guest reads and atomic publication of verified channel files."""

import hashlib
import os
from contextlib import ExitStack, suppress
from pathlib import Path, PurePosixPath
from uuid import uuid4

from app.records.channel_media import StoredFile
from app.services.openclaw_transcript import _open_contained
from app.services.storage_runtime.base import StorageReadLimitExceededError


def read_guest_file(root: Path, relative: str, limit: int) -> bytes:
    with _open_contained(root, PurePosixPath(relative)) as fd, os.fdopen(os.dup(fd), "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise StorageReadLimitExceededError(limit, len(data))
    return data


def publish_guest_file(root: Path, file: StoredFile, data: bytes) -> None:
    """Publish through directory descriptors, never following guest-controlled symlinks."""
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        relative = PurePosixPath(file.path)
        for part in relative.parts[:-1]:
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o755, dir_fd=directory)
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
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


def delete_guest_file(root: Path, relative: str) -> None:
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        path = PurePosixPath(relative)
        for part in path.parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        os.unlink(path.name, dir_fd=directory)
