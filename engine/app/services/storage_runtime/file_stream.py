"""Serve an open local file, or a stored object read in ranged chunks, as an HTTP response."""

import os
import re
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC
from email.utils import formatdate, parsedate_to_datetime
from typing import BinaryIO

from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from starlette.responses import Response, StreamingResponse

from app.core.logging import logger
from app.services.storage_runtime.base import StorageBackend, StorageReadChangedError, StorageVersion

_CHUNK_BYTES = 1024 * 1024
# One `bytes=` range of `a-b`, `a-` or `-n`. Eighteen digits per bound keep `int` far from any limit; a longer number is
# treated as malformed, and a malformed range is ignored.
_BYTE_RANGE = re.compile(r"bytes=([0-9]{0,18})-([0-9]{0,18})")
# The files are behind a login: a shared cache must not keep them, and a browser must revalidate before reusing one.
_CACHE_CONTROL = "private, no-cache"


def stream_chunks(stream: BinaryIO, size: int) -> Iterator[bytes]:
    """Read at most `size` bytes, so a file that grew after its length was announced cannot overrun it."""
    with stream:
        remaining = size
        while remaining > 0 and (chunk := stream.read(min(_CHUNK_BYTES, remaining))):
            remaining -= len(chunk)
            yield chunk


def file_response(
    request_headers: Headers,
    stream: BinaryIO,
    info: os.stat_result,
    *,
    media_type: str,
    headers: Mapping[str, str],
) -> Response:
    """Answer a GET for the open file `stream` (`info` is its `fstat`) with validators, one byte range or a 304.

    The ETag is derived from the modification time and size alone, so nothing is hashed. `If-None-Match` (weak
    comparison) decides a 304, and only without it `If-Modified-Since` does. A single `bytes=` range gets a 206, or a
    416 when nothing in the file can satisfy it; any other `Range` is ignored, and so is one whose `If-Range` is not
    the exact ETag or a strong Last-Modified value. `headers` go on the 200 and 206 only. The stream is closed here for a 304 or
    416; otherwise the body closes it, and a background task does when the body never starts.
    """
    etag = f'"{info.st_mtime_ns:x}-{info.st_size:x}"'
    modified = formatdate(info.st_mtime, usegmt=True)
    validators = {"Accept-Ranges": "bytes", "ETag": etag, "Last-Modified": modified, "Cache-Control": _CACHE_CONTROL}
    if _is_not_modified(request_headers, etag, info.st_mtime):
        stream.close()
        return Response(status_code=304, headers=validators)
    size = info.st_size
    # RFC 9110 8.8.2.2: two writes within one second share a Last-Modified, so the date is a strong validator only
    # once the file is a second old. A weak date must not resume a range, or the client splices two versions together.
    strong_modified = modified if info.st_mtime + 1 <= time.time() else None
    selected = _requested_range(request_headers, etag, strong_modified, size)
    if selected is not None and not selected:
        stream.close()
        return Response(status_code=416, headers={**validators, "Content-Range": f"bytes */{size}"})
    response_headers = {**headers, **validators}
    if selected is None:
        status, length = 200, size
    else:
        status, length = 206, len(selected)
        response_headers["Content-Range"] = f"bytes {selected.start}-{selected.stop - 1}/{size}"
        _ = stream.seek(selected.start)
    response_headers["Content-Length"] = str(length)
    return StreamingResponse(
        stream_chunks(stream, length),
        status_code=status,
        media_type=media_type,
        headers=response_headers,
        background=BackgroundTask(stream.close),
    )


def storage_response(
    storage: StorageBackend, key: str, version: StorageVersion, *, media_type: str, headers: Mapping[str, str]
) -> Response:
    """Answer a GET with the whole object `version` describes, announcing its size and reading it chunk by chunk."""
    return StreamingResponse(
        _storage_chunks(storage, key, version),
        media_type=media_type,
        headers={**headers, "Content-Length": str(version.size)},
    )


async def _storage_chunks(storage: StorageBackend, key: str, version: StorageVersion) -> AsyncIterator[bytes]:
    """Read `version.size` bytes in ranged reads of at most `_CHUNK_BYTES`, every one from `version`.

    A chunk is sent only after the key is looked up again and still carries `version`'s token, so bytes of two
    versions never reach the client. A changed key or a short read raises before the chunk goes out, and the
    transfer ends short of its `Content-Length`.
    """
    position = 0
    while position < version.size:
        wanted = min(_CHUNK_BYTES, version.size - position)
        chunk = await storage.read_bytes_bounded(key, wanted, offset=position)
        if len(chunk) != wanted or (await storage.get_version(key)).token != version.token:
            logger.warning(f"Download of {key} stopped at byte {position} of {version.size}: the file changed")
            raise StorageReadChangedError(key)
        position += wanted
        yield chunk


def _is_not_modified(request_headers: Headers, etag: str, mtime: float) -> bool:
    if (tags := request_headers.get("if-none-match")) is not None:
        listed = {tag.strip().removeprefix("W/") for tag in tags.split(",")}
        return "*" in listed or etag in listed
    if (since := request_headers.get("if-modified-since")) is None:
        return False
    try:
        date = parsedate_to_datetime(since)
    except TypeError, ValueError:
        return False
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return date.timestamp() >= int(mtime)


def _requested_range(request_headers: Headers, etag: str, modified: str | None, size: int) -> range | None:
    """The bytes asked for, empty when the file has none of them, or None when the whole file is to be sent."""
    requested = _BYTE_RANGE.fullmatch(request_headers.get("range", ""))
    if requested is None or request_headers.get("if-range", etag) not in {etag, modified}:
        return None
    first, last = requested.groups()
    if not first and not last:
        return None
    if not first:
        return range(size - min(int(last), size), size)
    if last and int(first) > int(last):
        return None
    return range(int(first), min(int(last) + 1, size) if last else size)
