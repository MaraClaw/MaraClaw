from __future__ import annotations

import gzip
import tracemalloc
import zlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import override

import httpx
import pytest

from app.services.channels import media_limits
from app.services.channels.media_limits import (
    EVENT_MAX_BYTES,
    READ_CHUNK_BYTES,
    MediaBudget,
    MediaTooLargeError,
    file_limit,
    read_capped,
)
from app.services.document_parser import IMAGE_UPLOAD_MAX_BYTES, MAX_INPUT_BYTES
from channel_media_fixtures import CountingStream

LIMIT = 1024 * 1024


async def read(handler: Callable[[httpx.Request], httpx.Response], limit: int = LIMIT) -> bytes:
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        client.stream("GET", "https://files.invalid/f") as response,
    ):
        return await read_capped(response, limit)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("photo.png", IMAGE_UPLOAD_MAX_BYTES),
        ("PHOTO.JPEG", IMAGE_UPLOAD_MAX_BYTES),
        ("screen-recording.mp4", MAX_INPUT_BYTES),
        ("logs.tar.gz", MAX_INPUT_BYTES),
        (".png", MAX_INPUT_BYTES),
        ("noextension", MAX_INPUT_BYTES),
    ],
)
def test_file_limit_picks_the_web_chat_cap_by_extension(filename, expected):
    # When / Then
    assert file_limit(filename) == expected


def test_event_budget_is_the_largest_single_web_upload():
    # Given
    budget = MediaBudget()
    # When
    budget.spend(EVENT_MAX_BYTES - 5)
    # Then: a file is limited by what the event has left, never above its own cap
    assert EVENT_MAX_BYTES == MAX_INPUT_BYTES
    assert budget.limit_for("photo.png") == 5
    assert MediaBudget().limit_for("photo.png") == IMAGE_UPLOAD_MAX_BYTES


async def test_read_capped_stops_one_chunk_past_the_limit_without_content_length():
    # Given: a body ten times the limit that never declares its size
    stream = CountingStream(total=10 * LIMIT)
    # When
    with pytest.raises(MediaTooLargeError) as raised:
        await read(lambda _: httpx.Response(200, stream=stream))
    # Then: nothing past the limit plus one read chunk was pulled
    assert raised.value.limit == LIMIT
    assert LIMIT < stream.consumed <= LIMIT + READ_CHUNK_BYTES
    assert raised.value.read == stream.consumed


async def test_read_capped_holds_the_body_once():
    # Given: an 8 MiB body in read-sized blocks
    size = 8 * 1024 * 1024
    # When
    tracemalloc.start()
    try:
        data = await read(lambda _: httpx.Response(200, stream=CountingStream(total=size)), limit=size)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    # Then: a list of chunks joined at the end would peak at twice the body
    assert len(data) == size
    assert peak <= size + READ_CHUNK_BYTES


def test_a_spent_event_budget_refuses_the_next_file_instead_of_requesting_it():
    # Given: an aborted read overshoots what the event had left
    budget = MediaBudget(remaining=LIMIT)
    budget.spend(LIMIT + READ_CHUNK_BYTES)
    # When / Then
    with pytest.raises(MediaTooLargeError) as raised:
        budget.limit_for("notes.txt")
    assert raised.value.limit == 0
    assert budget.remaining == 0


async def test_read_capped_returns_a_body_of_exactly_the_limit():
    # Given
    stream = CountingStream(total=LIMIT)
    # When
    data = await read(lambda _: httpx.Response(200, stream=stream))
    # Then
    assert len(data) == LIMIT


async def test_read_capped_rejects_a_declared_oversize_before_reading_the_body():
    # Given
    stream = CountingStream(total=2 * LIMIT)
    # When
    with pytest.raises(MediaTooLargeError) as raised:
        await read(lambda _: httpx.Response(200, headers={"content-length": str(2 * LIMIT)}, stream=stream))
    # Then
    assert stream.consumed == 0
    assert raised.value.read == 0


async def test_read_capped_counts_decoded_bytes_not_wire_bytes():
    # Given: a body that is tiny on the wire but twice the limit once decoded
    wire = gzip.compress(b"\0" * (2 * LIMIT))
    assert len(wire) < LIMIT
    # When / Then
    with pytest.raises(MediaTooLargeError):
        await read(lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, content=wire))


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
async def test_compressed_stream_bounds_combined_peak_before_aborting(encoding, record_property):
    # Given: the first wire chunk expands to sixteen times the cap; later chunks must stay unread.
    payload = b"x" * (16 * LIMIT)
    wire = gzip.compress(payload) if encoding == "gzip" else zlib.compress(payload)
    if encoding == "raw-deflate":
        wire = wire[2:-4]
    del payload

    class Wire(httpx.AsyncByteStream):
        consumed: int = 0

        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(10):
                self.consumed += 1
                # Allocate inside the measured interval, including the wire buffer retained by HTTPX at yield.
                yield memoryview(wire).tobytes()

    stream = Wire()
    # When: use an actual streaming Response, so HTTPX cannot predecode the fixture.
    tracemalloc.start()
    try:
        with pytest.raises(MediaTooLargeError) as raised:
            await read(
                lambda _: httpx.Response(
                    200,
                    headers={"content-encoding": "deflate" if encoding == "raw-deflate" else encoding},
                    stream=stream,
                )
            )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    record_property("peak_bytes", peak)
    # Then: the whole allocation peak fits, including wire, tail, output, body, metadata and decoder state.
    assert peak <= LIMIT + READ_CHUNK_BYTES
    assert stream.consumed == 1
    assert LIMIT < raised.value.read <= LIMIT + READ_CHUNK_BYTES
    budget = MediaBudget(remaining=LIMIT)
    budget.spend(raised.value.read)
    assert budget.remaining == 0


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("size", [LIMIT, IMAGE_UPLOAD_MAX_BYTES, MAX_INPUT_BYTES])
async def test_compressed_stream_returns_exact_limit_with_bounded_combined_peak(encoding, size, record_property):
    # Given: a valid compressed body ending exactly at the decoded cap.
    payload = b"x" * size
    wire = gzip.compress(payload) if encoding == "gzip" else zlib.compress(payload)
    if encoding == "raw-deflate":
        wire = wire[2:-4]

    class Wire(httpx.AsyncByteStream):
        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for offset in range(0, len(wire), 31):
                yield wire[offset : offset + 31]

    # When: fixture and expected payload predate tracing; streamed wire slices are allocated under tracing.
    tracemalloc.start()
    try:
        data = await read(
            lambda _: httpx.Response(
                200, headers={"content-encoding": "deflate" if encoding == "raw-deflate" else encoding}, stream=Wire()
            ),
            limit=size,
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    record_property("peak_bytes", peak)
    # Then: this is stricter than the payload-only contract (fixed metadata and decoder state are also counted).
    assert data == payload
    assert peak <= size + READ_CHUNK_BYTES


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("same_chunk", [False, True])
async def test_compressed_trailing_input_is_rejected_with_bounded_memory(encoding, same_chunk):
    # Given: a completed stream followed by many individually permitted wire chunks.
    wire = gzip.compress(b"small") if encoding == "gzip" else zlib.compress(b"small")
    if encoding == "raw-deflate":
        wire = wire[2:-4]

    class Wire(httpx.AsyncByteStream):
        consumed: int = 0

        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.consumed += 1
            yield wire + (b"trailing" if same_chunk else b"")
            for _ in range(100):
                self.consumed += 1
                yield b"x" * READ_CHUNK_BYTES

    stream = Wire()
    # When: trace wire allocation too, so accumulating zlib.unused_data violates the bound.
    tracemalloc.start()
    try:
        with pytest.raises(httpx.DecodingError, match="Trailing"):
            await read(
                lambda _: httpx.Response(
                    200,
                    headers={"content-encoding": "deflate" if encoding == "raw-deflate" else encoding},
                    stream=stream,
                )
            )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    # Then: no completed decoder accumulates trailing input, in this chunk or a later one.
    assert stream.consumed == (1 if same_chunk else 2)
    assert peak <= LIMIT + READ_CHUNK_BYTES


@pytest.mark.parametrize("total", [7, 2 * LIMIT])
async def test_stream_and_disk_file_are_released_on_success_and_oversize(monkeypatch, total):
    wire = gzip.compress(b"x" * total)
    finished = []

    class Wire(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.chunks = self._chunks()

        @override
        def __aiter__(self) -> AsyncIterator[bytes]:
            return self.chunks

        @override
        async def aclose(self) -> None:
            await self.chunks.aclose()

        async def _chunks(self) -> AsyncGenerator[bytes]:
            try:
                yield wire
            finally:
                finished.append(True)

    with media_limits.tempfile.TemporaryFile(mode="w+b", buffering=0) as body:
        monkeypatch.setattr(media_limits.tempfile, "TemporaryFile", lambda **kwargs: body)
        limit = min(total, LIMIT)
        if total > limit:
            with pytest.raises(MediaTooLargeError):
                await read(lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Wire()), limit)
        else:
            assert (
                await read(lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Wire()), limit)
                == b"x" * total
            )
        assert body.closed
        assert finished == [True]


async def test_transport_yields_exceeding_the_scratch_contract_fail_before_decoding():
    class Wire(httpx.AsyncByteStream):
        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"x" * (READ_CHUNK_BYTES + 1)

    with pytest.raises(httpx.DecodingError, match="transport chunk"):
        await read(lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Wire()))


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, deflate", "unknown"])
async def test_unbounded_or_unknown_encodings_fail_before_reading(encoding):
    # Given / When
    stream = CountingStream(total=LIMIT)
    with pytest.raises(httpx.DecodingError):
        await read(lambda _: httpx.Response(200, headers={"content-encoding": encoding}, stream=stream))
    # Then
    assert stream.consumed == 0


@pytest.mark.parametrize("failure", ["status_503", "read_timeout"])
async def test_read_capped_lets_transient_failures_propagate(failure):
    # Given
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "read_timeout":
            raise httpx.ReadTimeout("slow provider", request=request)
        return httpx.Response(503)

    # When / Then: the provider keeps its retry signal
    with pytest.raises(httpx.ReadTimeout if failure == "read_timeout" else httpx.HTTPStatusError):
        await read(handler)
