"""Serve an open local file as an HTTP body without reopening it by name."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from app.services.storage_runtime.bounded import open_regular_file

_CHUNK_BYTES = 1024 * 1024


def stream_chunks(stream: BinaryIO, size: int) -> Iterator[bytes]:
    """Read at most `size` bytes, so a file that grew after its length was announced cannot overrun it."""
    with stream:
        remaining = size
        while remaining > 0 and (chunk := stream.read(min(_CHUNK_BYTES, remaining))):
            remaining -= len(chunk)
            yield chunk


def open_contained_stream(path: Path) -> tuple[BinaryIO, int]:
    """Open `path` through an O_NOFOLLOW walk (a link anywhere on it is refused) and return a stream and its size."""
    with open_regular_file(path) as source:
        return os.fdopen(os.dup(source.fileno()), "rb"), os.fstat(source.fileno()).st_size
