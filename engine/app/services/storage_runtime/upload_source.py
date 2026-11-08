"""An opened local file prepared for a streaming create: size and MD5 are fixed before any provider call."""

import hashlib
import os
import stat
from dataclasses import dataclass
from io import FileIO
from pathlib import Path
from types import TracebackType
from typing import Final, Self

_HASH_CHUNK_BYTES: Final = 1024 * 1024


class UploadSourceChangedError(OSError):
    """The file ended before the size read from its open handle; nothing was reserved or sent."""

    def __init__(self, path: Path, expected: int, actual: int):
        self.path: Path = path
        self.expected: int = expected
        self.actual: int = actual
        super().__init__(f"{path} held {actual} of {expected} bytes while being prepared for upload")


class SizedBody:
    """Seekable read-only window over the first ``size`` bytes of an open file, so a client sends exactly that many."""

    def __init__(self, stream: FileIO, size: int):
        self._stream: FileIO = stream
        self._size: int = size

    def read(self, amt: int | None = -1) -> bytes:
        remaining = self._size - self._stream.tell()
        if remaining <= 0:
            return b""
        return bytes(self._stream.read(remaining if amt is None or amt < 0 else min(amt, remaining)))

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        match whence:
            case os.SEEK_SET:
                target = offset
            case os.SEEK_CUR:
                target = self._stream.tell() + offset
            case os.SEEK_END:
                target = self._size + offset
            case _:
                raise ValueError(f"Unsupported whence: {whence}")
        return self._stream.seek(min(max(target, 0), self._size))

    def tell(self) -> int:
        return self._stream.tell()

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class UploadSource:
    """An open regular file with the size taken from its handle; ``md5`` is empty unless requested.

    Whoever sends the bytes owns ``close``, which is idempotent.
    """

    stream: FileIO
    size: int
    md5: str

    def body(self) -> SizedBody:
        _ = self.stream.seek(0)
        return SizedBody(self.stream, self.size)

    def close(self) -> None:
        self.stream.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()


def open_upload_source(path: Path, *, with_md5: bool) -> UploadSource:
    """Open ``path`` once; take the size from ``fstat`` and, when asked, the MD5 of exactly that many bytes."""
    stream = FileIO(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb")
    try:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise FileNotFoundError(path)
        md5 = _md5_of_prefix(path, stream, status.st_size) if with_md5 else ""
        _ = stream.seek(0)
    except BaseException:
        stream.close()
        raise
    return UploadSource(stream, status.st_size, md5)


def _md5_of_prefix(path: Path, stream: FileIO, size: int) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    remaining = size
    while remaining:
        chunk = stream.read(min(_HASH_CHUNK_BYTES, remaining))
        if not chunk:
            raise UploadSourceChangedError(path, size, size - remaining)
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()
