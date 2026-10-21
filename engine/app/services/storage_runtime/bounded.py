"""Actual-byte accounting shared by blocking storage readers."""

from typing import Protocol

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
