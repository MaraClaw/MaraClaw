"""``deadline_lines`` ends a response's line iterator at one wall-clock deadline that no line, keepalive or idle read resets."""

import asyncio
from collections.abc import AsyncIterator
from typing import override

import pytest

from app.services.llm.base import LLMError
from app.services.llm.caller import is_retryable_error
from app.services.llm.failover import FailoverErrorType, classify_error
from app.services.llm.stream_deadline import STREAM_DEADLINE_SECONDS, StreamDeadlineError, deadline_lines
from loop_clock_fixtures import freeze_loop_clock


async def lines_of(*lines: str) -> AsyncIterator[str]:
    for line in lines:
        yield line


async def read_one(lines: AsyncIterator[str]) -> str:
    return await anext(lines)


class RaisesTimeout(AsyncIterator[str]):
    @override
    async def __anext__(self) -> str:
        raise TimeoutError("socket")


async def test_lines_inside_the_deadline_pass_through_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = freeze_loop_clock(monkeypatch)

    assert [line async for line in deadline_lines(lines_of("a", "b", "c"), request_timeout=1)] == ["a", "b", "c"]


async def test_keepalives_that_never_suspend_cannot_outlast_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a source that answers every read at once, each answer taking a third of the deadline.
    clock = freeze_loop_clock(monkeypatch)
    sent = 0

    async def keepalives() -> AsyncIterator[str]:
        nonlocal sent
        while sent < 10:
            clock.jump(STREAM_DEADLINE_SECONDS / 3)
            sent += 1
            yield ": keepalive"

    # When it is read to the end.
    with pytest.raises(StreamDeadlineError):
        async for _line in deadline_lines(keepalives(), request_timeout=1):
            pass
    # Then the third keepalive spent the deadline and the fourth was never requested.
    assert sent == 3


async def test_an_idle_read_ends_at_the_deadline_and_cancels_its_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a source that goes quiet inside a read.
    clock = freeze_loop_clock(monkeypatch)
    parked, cancelled = asyncio.Event(), asyncio.Event()

    async def idle() -> AsyncIterator[str]:
        parked.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        yield "unreachable"

    reading = asyncio.create_task(read_one(deadline_lines(idle(), request_timeout=1)))
    await parked.wait()
    # When the loop's clock passes the deadline.
    await clock.advance(STREAM_DEADLINE_SECONDS)
    # Then the parked read ended as a stream deadline and its source was cancelled.
    assert reading.done()
    with pytest.raises(StreamDeadlineError):
        reading.result()
    assert cancelled.is_set()


async def test_outer_cancellation_stays_a_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = freeze_loop_clock(monkeypatch)
    parked, cancelled = asyncio.Event(), asyncio.Event()

    async def idle() -> AsyncIterator[str]:
        parked.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        yield "unreachable"

    reading = asyncio.create_task(read_one(deadline_lines(idle(), request_timeout=1)))
    await parked.wait()

    _ = reading.cancel()

    with pytest.raises(asyncio.CancelledError):
        await reading
    assert cancelled.is_set()


async def test_a_timeout_the_source_raises_itself_is_not_taken_for_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = freeze_loop_clock(monkeypatch)

    with pytest.raises(TimeoutError, match="socket") as raised:
        _ = await anext(deadline_lines(RaisesTimeout(), request_timeout=1))

    assert not isinstance(raised.value, StreamDeadlineError)


async def test_the_clients_request_timeout_raises_the_floor_of_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a client configured with twice the default total.
    clock = freeze_loop_clock(monkeypatch)
    lines = deadline_lines(lines_of("a", "b"), request_timeout=2 * STREAM_DEADLINE_SECONDS)
    # When the default total has passed, the stream is still alive; when the configured one has, it is not.
    clock.jump(STREAM_DEADLINE_SECONDS + 1)
    assert await anext(lines) == "a"
    clock.jump(STREAM_DEADLINE_SECONDS)
    with pytest.raises(StreamDeadlineError):
        _ = await anext(lines)


def test_the_error_is_an_llm_error_failover_reads_as_a_provider_timeout() -> None:
    error = StreamDeadlineError(STREAM_DEADLINE_SECONDS)

    assert isinstance(error, LLMError)
    assert classify_error(error) is FailoverErrorType.RETRYABLE
    assert is_retryable_error(f"[LLM Error] {error}")
