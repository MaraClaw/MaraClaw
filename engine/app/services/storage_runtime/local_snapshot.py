"""Descriptor-owned bounded local snapshots."""

import os
from pathlib import Path

from app.services.storage_runtime.base import (
    StorageReadChangedError,
    StorageReadLimitExceededError,
    StorageSnapshot,
    StorageVersion,
    content_hash_bytes,
)
from app.services.storage_runtime.bounded import read_bounded
from app.services.storage_runtime.local_contained import open_regular_entry


def read_file_snapshot(root: Path, relative: str, max_bytes: int) -> StorageSnapshot:
    """Read and hash once, rejecting a change of the descriptor or of the entry it was opened from."""
    with open_regular_entry(root, relative) as (stream, directory, name):
        before = os.fstat(stream.fileno())
        if before.st_size > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, before.st_size)
        data = read_bounded(stream, max_bytes)
        digest = content_hash_bytes(data)
        after = os.fstat(stream.fileno())
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            raise StorageReadChangedError(relative) from None
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
            or len(data) != after.st_size
        ):
            raise StorageReadChangedError(relative)
    return StorageSnapshot(
        data=data,
        version=StorageVersion(
            key=relative,
            exists=True,
            is_dir=False,
            size=len(data),
            modified_at=str(after.st_mtime),
            etag=digest,
            version_id=f"{after.st_mtime_ns}:{after.st_size}:{digest}",
            content_hash=digest,
        ),
        sha256=digest,
    )


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns
