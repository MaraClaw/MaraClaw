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
from app.services.storage_runtime.bounded import open_regular_file, read_bounded


def read_file_snapshot(path: Path, key: str, max_bytes: int) -> StorageSnapshot:
    """Read and hash once, rejecting detected descriptor or path changes."""
    with open_regular_file(path) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, before.st_size)
        data = read_bounded(stream, max_bytes)
        digest = content_hash_bytes(data)
        after = os.fstat(stream.fileno())
        try:
            current = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            raise StorageReadChangedError(key) from None
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
            or len(data) != after.st_size
        ):
            raise StorageReadChangedError(key)
    return StorageSnapshot(
        data=data,
        version=StorageVersion(
            key=key,
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
