"""Actual-byte accounting shared by blocking storage readers."""

import errno
import os
import stat
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from io import FileIO
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException

from app.services.storage_runtime.base import StorageReadLimitExceededError


class BoundedReader(Protocol):
    def read(self, size: int, /) -> bytes: ...


def read_bounded(stream: BoundedReader, max_bytes: int) -> bytes:
    if max_bytes < 0:
        raise ValueError("max_bytes must be nonnegative")
    data = bytearray()
    while True:
        chunk = stream.read(min(64 * 1024, max_bytes + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, len(data))


@contextmanager
def open_regular_file(path: Path) -> Generator[FileIO]:
    # Anchor every component with O_NOFOLLOW: a concurrent symlink swap must not cross agent roots.
    with ExitStack() as stack:
        parent_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, parent_fd)
        try:
            for component in path.parts[1:-1]:
                parent_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                stack.callback(os.close, parent_fd)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise HTTPException(403, "Symlink previews are not allowed") from exc
            raise
        with FileIO(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise FileNotFoundError(path)
            yield stream
