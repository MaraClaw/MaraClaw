"""One wall-clock deadline for a provider's whole streamed response.

httpx restarts its read timeout with every read, so a server that keeps sending SSE keepalives can hold a stream open as
long as it likes. ``deadline_lines`` wraps a response's line iterator with a deadline on the event loop's clock that
nothing resets: not a keepalive, not a trickle of lines, not an idle read. The deadline counts from the moment the
iterator is wrapped, which is when the response headers have arrived; waiting for them stays under httpx's own timeouts.

Expiry raises ``StreamDeadlineError``, an ``LLMError`` whose message names a timeout, so ``failover.classify_error``
reads it as a provider timeout: the provider's circuit counts it and ``call_llm_with_failover`` falls back once. It is
not in ``retry_policy.RETRYABLE_TRANSPORT_ERRORS``, so the provider call that raises it never retries it. Tests move
the clock by freezing ``loop.time`` (``tests/loop_clock_fixtures.py``).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Final, override

from app.services.llm.base import LLMError

STREAM_DEADLINE_SECONDS: Final = 900.0


class StreamDeadlineError(LLMError):
    """A streamed response was not complete when its wall-clock deadline passed."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"LLM stream timeout: the response was not complete within {seconds:g} seconds")


class _DeadlineLines(AsyncIterator[str]):
    def __init__(self, lines: AsyncIterator[str], seconds: float) -> None:
        self._lines: AsyncIterator[str] = lines
        self._seconds: float = seconds
        self._loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self._deadline: float = self._loop.time() + seconds

    @override
    async def __anext__(self) -> str:
        # A source that answers without suspending never lets the timer below run, so the clock is read first.
        if self._loop.time() >= self._deadline:
            raise StreamDeadlineError(self._seconds)
        scope = asyncio.timeout_at(self._deadline)
        try:
            async with scope:
                return await anext(self._lines)
        except TimeoutError as error:
            if scope.expired():
                raise StreamDeadlineError(self._seconds) from error
            raise


def deadline_lines(lines: AsyncIterator[str], *, request_timeout: float) -> AsyncIterator[str]:
    """``lines`` under one deadline of ``STREAM_DEADLINE_SECONDS``, or the client's ``request_timeout`` when longer.

    A stream's total never undercuts the per-read timeout its own client was configured with.
    """
    return _DeadlineLines(lines, max(STREAM_DEADLINE_SECONDS, request_timeout))
