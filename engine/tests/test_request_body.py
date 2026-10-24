from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.core.request_body import read_bounded_body


@pytest.mark.parametrize("length", [None, "8", "1", "invalid", "-1"])
async def test_bounded_reader_stops_on_first_overflow_chunk(length):
    # Given: an untrusted length and an unread sentinel after the exceeding chunk.
    headers = [] if length is None else [(b"content-length", length.encode())]
    receive = AsyncMock(
        side_effect=[
            {"type": "http.request", "body": b"12345678", "more_body": True},
            {"type": "http.request", "body": b"9", "more_body": True},
            AssertionError("Overflow must not drain the stream"),
        ]
    )
    request = Request({"type": "http", "headers": headers}, receive)

    # When: the body crosses its eight-byte budget.
    body = await read_bounded_body(request, 8)

    # Then: no partial body is returned or cached, and the sentinel stays unread.
    assert body is None
    assert receive.await_count == 2
    assert not hasattr(request, "_body")


async def test_bounded_reader_rejects_declared_overflow_without_reading():
    # Given: an oversized declared length, even if the actual body could be empty.
    receive = AsyncMock(side_effect=AssertionError("Declared overflow must not read"))
    request = Request({"type": "http", "headers": [(b"content-length", b"9")]}, receive)

    # When: checking a smaller budget.
    body = await read_bounded_body(request, 8)

    # Then: the receive channel is untouched.
    assert body is None
    receive.assert_not_called()


@pytest.mark.parametrize("length", [None, "7", "invalid"])
async def test_bounded_reader_caches_exact_boundary_for_body_and_json(length):
    # Given: a JSON document split across chunks, exactly at its byte budget.
    headers = [] if length is None else [(b"content-length", length.encode())]
    receive = AsyncMock(
        side_effect=[
            {"type": "http.request", "body": b'{"x":', "more_body": True},
            {"type": "http.request", "body": b"1}", "more_body": False},
        ]
    )
    request = Request({"type": "http", "headers": headers}, receive)

    # When: the bounded read consumes the stream.
    body = await read_bounded_body(request, 7)

    # Then: downstream Starlette consumers use the bounded cache without receiving again.
    assert body == b'{"x":1}'
    assert await request.body() == body
    assert await request.json() == {"x": 1}
    assert await read_bounded_body(request, 7) == body
    assert receive.await_count == 2


async def test_bounded_reader_accepts_empty_body_at_zero_budget():
    # Given: an empty final ASGI message.
    receive = AsyncMock(return_value={"type": "http.request", "body": b"", "more_body": False})
    request = Request({"type": "http", "headers": []}, receive)

    # When: reading with no byte budget.
    body = await read_bounded_body(request, 0)

    # Then: an empty body remains distinguishable from overflow.
    assert body == b""
    assert await request.body() == b""
