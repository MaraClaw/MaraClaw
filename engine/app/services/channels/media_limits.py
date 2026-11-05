"""Size limits and streamed downloads for inbound channel attachments.

Web chat caps an upload at ``IMAGE_UPLOAD_MAX_BYTES`` for images and ``MAX_INPUT_BYTES`` for everything else, picked by
filename extension (``app/api/upload.py``). Channel media follows the same decision, but is streamed: decoded bytes are
counted as they arrive and the read stops on the first decoded chunk past the limit. Decoding itself is bounded before
allocation. ``Content-Length`` is only an early reject, never an admission ticket.
"""

from __future__ import annotations

import io
import tempfile
import zlib
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Final, override

import httpx

from app.core.json_types import http_header
from app.services.document_parser import IMAGE_EXTENSIONS, IMAGE_UPLOAD_MAX_BYTES, MAX_INPUT_BYTES

# One event may carry many files. Charge successful and aborted reads against the largest web upload allowance.
# The final read may exceed the allowance by one decoded chunk; exhausted events make no further media requests.
EVENT_MAX_BYTES: Final = MAX_INPUT_BYTES
READ_CHUNK_BYTES: Final = 64 * 1024
_SPOOL_BUFFER_BYTES: Final = 8 * 1024
_MIB: Final = 1024 * 1024


@dataclass(slots=True)
class MediaTooLargeError(Exception):
    """A download passed its limit. Deterministic for a given file, so callers skip it instead of retrying.

    ``read`` is the decoded bytes pulled off the wire before the read was abandoned; callers charge it to the event.
    """

    limit: int
    read: int = 0

    @override
    def __str__(self) -> str:
        return f"Attachment is larger than {_size(self.limit)}"


def file_limit(filename: str) -> int:
    """Web chat's per-file decision: image extensions get the image cap, everything else the file cap."""
    extension = Path(filename.lstrip(".")).suffix.lower()
    return IMAGE_UPLOAD_MAX_BYTES if extension in IMAGE_EXTENSIONS else MAX_INPUT_BYTES


@dataclass(slots=True)
class MediaBudget:
    """Decoded-byte allowance shared by every attachment of one native event."""

    remaining: int = EVENT_MAX_BYTES

    def limit_for(self, filename: str) -> int:
        """Largest body to accept for ``filename``; raises once the event is spent so no further request is made."""
        limit = min(file_limit(filename), self.remaining)
        if limit <= 0:
            raise MediaTooLargeError(limit=0)
        return limit

    def spend(self, size: int) -> None:
        """Charge bytes actually read, whether or not the file was kept."""
        self.remaining = max(0, self.remaining - size)


async def read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read an open streaming response, raising ``MediaTooLargeError`` the moment decoded bytes pass ``limit``.

    HTTP errors (including 5xx) and timeouts propagate unchanged. A disk file with an 8 KiB buffer holds the body until
    decoding finishes; all streaming generators are released before the single final allocation of at most ``limit`` bytes.
    With C = min(64 KiB, (limit + 64 KiB - 8 KiB) // 4), compressed transport yields must be at most C bytes.
    Identity yields may use min(64 KiB, 4C), since they have no decoder copies. Both bounds are checked before decoding.
    Wire, old/new unconsumed tails, decoder output and the file buffer use at most 4C + 8 KiB <= limit + 64 KiB.
    The final body coexists only with the 8 KiB file buffer, also within that combined payload bound.
    This excludes fixed Python metadata, decompressor state and OS disk cache. HTTPX retains native transport yields;
    a consumer cannot prevent an upstream transport from allocating an oversized yield before this check sees it.
    Attachment HTTP policy accepts identity, gzip and deflate only; other/stacked encodings fail closed with DecodingError
    before reading. In particular, Brotli's output-buffer limit is not an exact bound. Never fall back to HTTPX decoding.
    """
    _ = response.raise_for_status()
    declared = http_header(response.headers, "content-length")
    if declared.isdecimal() and int(declared) > limit:
        raise MediaTooLargeError(limit)
    if response.is_stream_consumed:
        # HTTPX may already have decoded an in-memory response before this helper was called; do not copy it again.
        content = response.content
        if len(content) > limit:
            raise MediaTooLargeError(limit, read=min(len(content), limit + READ_CHUNK_BYTES))
        return content
    chunk_size = min(READ_CHUNK_BYTES, (limit + READ_CHUNK_BYTES - _SPOOL_BUFFER_BYTES) // 4)
    with (
        tempfile.TemporaryFile(mode="w+b", buffering=0) as spool,
        io.BufferedRandom(spool, buffer_size=_SPOOL_BUFFER_BYTES) as body,
    ):
        async with aclosing(_bounded_chunks(response, chunk_size)) as chunks:
            async for chunk in chunks:
                total = body.tell() + len(chunk)
                if total > limit:
                    raise MediaTooLargeError(limit, read=total)
                _ = body.write(chunk)
                del chunk
        size = body.tell()
        _ = body.seek(0)
        return body.read(size)


async def _bounded_chunks(response: httpx.Response, chunk_size: int) -> AsyncGenerator[bytes | memoryview]:
    """Decode raw wire bytes with a hard output bound; never use an unbounded decoder flush."""
    encoding = http_header(response.headers, "content-encoding").strip().lower()
    if encoding not in {"", "identity", "gzip", "deflate"}:
        raise httpx.DecodingError(f"Unsupported attachment Content-Encoding: {encoding}", request=response.request)
    compressed = encoding in {"gzip", "deflate"}
    wire_limit = chunk_size if compressed else min(READ_CHUNK_BYTES, 4 * chunk_size)
    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16 if encoding == "gzip" else zlib.MAX_WBITS)
    first_attempt = encoding == "deflate"
    wire_chunks = response.aiter_raw()
    if not isinstance(wire_chunks, AsyncGenerator):
        raise httpx.StreamError("Attachment raw iterator must support explicit generator cleanup")
    async with aclosing(wire_chunks) as wire:
        async for raw in wire:
            if len(raw) > wire_limit:
                raise httpx.DecodingError("Attachment transport chunk exceeds memory bound", request=response.request)
            if not compressed:
                yield memoryview(raw)
                continue
            if decoder.eof:
                raise httpx.DecodingError("Trailing attachment compressed data", request=response.request)
            pending = raw
            while True:
                try:
                    chunk = decoder.decompress(pending, chunk_size)
                except zlib.error as exc:
                    if not first_attempt:
                        raise httpx.DecodingError(str(exc), request=response.request) from exc
                    # HTTPX also accepts the common nonstandard raw-deflate HTTP representation.
                    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                    first_attempt = False
                    continue
                first_attempt = False
                if decoder.unused_data:
                    raise httpx.DecodingError("Trailing attachment compressed data", request=response.request)
                pending = decoder.unconsumed_tail
                output_full = len(chunk) == chunk_size
                yield chunk
                del chunk
                if decoder.eof or (not pending and not output_full):
                    break


def too_large_note(filename: str, limit: int) -> str:
    """Short transcript marker for a skipped file."""
    return f"[file skipped: {filename} exceeds the {_limit_label(filename, limit)}]"


def too_large_reply(filename: str, limit: int) -> str:
    """Sender-facing text for a skipped file."""
    return (
        f"Sorry, {filename} exceeds the {_limit_label(filename, limit)}, so I did not download it. "
        "Please send a smaller file."
    )


def _limit_label(filename: str, limit: int) -> str:
    if limit < file_limit(filename):
        return f"{_size(EVENT_MAX_BYTES)} per-message attachment limit"
    return f"{_size(limit)} file limit"


def _size(size: int) -> str:
    return f"{round(size / _MIB, 1):g} MiB"
