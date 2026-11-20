"""Bounded, Retry-After-aware retries shared by the LLM provider clients.

Each provider module wraps its own request attempt in :func:`call_with_retry`. ``caller.py``,
``failover.py`` and ``http_pool.py`` never retry, so the fallback model stays reserved for a
provider that is actually down.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx

from app.core.logging import logger
from app.services.llm.base import ChunkCallback, LLMError, ThinkingCallback, ToolCallback

type Sleep = Callable[[float], Awaitable[None]]

# Transient provider conditions: 429 rate limit, 5xx, and Anthropic's 529 overloaded. Every other 4xx is the
# caller's mistake and is raised on the first attempt.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})
RETRYABLE_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Attempt budget and wait rules. ``sleep``, ``jitter`` and ``now`` are the injectable seams."""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    max_retry_after: float = 30.0
    sleep: Sleep = asyncio.sleep
    jitter: Callable[[], float] = random.random
    now: Callable[[], datetime] = _utcnow

    def delay_for(self, attempt: int, retry_after: str | None) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based).

        A usable ``Retry-After`` sets the wait, capped at ``max_retry_after``. Without one the wait is
        exponential backoff capped at ``max_delay``, scaled by jitter into [0.5, 1.0] of its size.
        """
        hinted = self._retry_after_seconds(retry_after)
        if hinted is not None:
            return min(hinted, self.max_retry_after)
        backoff = min(self.base_delay * 2.0 ** (attempt - 1), self.max_delay)
        return backoff * (0.5 + self.jitter() / 2)

    def _retry_after_seconds(self, value: str | None) -> float | None:
        """Parse delta-seconds or an HTTP-date; garbage, negative and past values are ignored."""
        if value is None:
            return None
        text = value.strip()
        try:
            seconds = float(text)
        except ValueError:
            try:
                when = parsedate_to_datetime(text)
            except ValueError:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - self.now()).total_seconds()
        return seconds if math.isfinite(seconds) and seconds >= 0 else None


RETRY_POLICY = RetryPolicy()


class LLMHTTPError(LLMError):
    """The provider answered with an HTTP error status.

    The message keeps the ``HTTP <status>: <body>`` shape that ``failover.classify_error`` reads.
    ``retry_after`` is the raw header value; the policy parses it.
    """

    def __init__(self, status_code: int, body: str, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status_code}: {body[:500]}")
        self.status_code: int = status_code
        self.retry_after: str | None = retry_after


def _retry_after_header(response: httpx.Response) -> str | None:
    return cast("Mapping[str, str]", response.headers).get("retry-after")


async def raise_for_status(response: httpx.Response) -> None:
    """Close a fully read response and raise ``LLMHTTPError`` when its status is an error."""
    if response.status_code >= 400:
        error = LLMHTTPError(response.status_code, response.text, _retry_after_header(response))
        await response.aclose()
        raise error


async def stream_http_error(response: httpx.Response) -> LLMHTTPError:
    """Read an error status's body from an open stream. The caller raises it; its ``async with`` closes the stream."""
    body = b"".join([chunk async for chunk in response.aiter_bytes()])
    return LLMHTTPError(response.status_code, body.decode(errors="replace"), _retry_after_header(response))


class StreamDelivery:
    """Wraps one stream call's callbacks and records whether any of them has fired.

    A stream is retried only while ``delivered`` is false, so nothing the caller already acted on is replayed.
    """

    def __init__(
        self,
        on_chunk: ChunkCallback | None,
        on_tool_delta: ToolCallback | None,
        on_thinking: ThinkingCallback | None,
    ) -> None:
        self.delivered: bool = False
        self.on_chunk: ChunkCallback | None = self._track(on_chunk)
        self.on_tool_delta: ToolCallback | None = self._track(on_tool_delta)
        self.on_thinking: ThinkingCallback | None = self._track(on_thinking)

    def _track[Arg](
        self, callback: Callable[[Arg], Coroutine[Any, Any, None]] | None
    ) -> Callable[[Arg], Coroutine[Any, Any, None]] | None:
        if callback is None:
            return None

        async def tracked(arg: Arg) -> None:
            self.delivered = True
            await callback(arg)

        return tracked


def _can_retry(attempt: int, policy: RetryPolicy, delivery: StreamDelivery | None) -> bool:
    return attempt < policy.max_attempts and (delivery is None or not delivery.delivered)


async def call_with_retry[T](
    attempt: Callable[[], Awaitable[T]],
    *,
    provider: str,
    delivery: StreamDelivery | None = None,
    policy: RetryPolicy | None = None,
) -> T:
    """Run ``attempt`` until it succeeds or the attempt budget is spent.

    Only ``RETRYABLE_TRANSPORT_ERRORS`` and ``LLMHTTPError`` with a ``RETRYABLE_STATUS_CODES`` status are
    retried, and a stream never once ``delivery`` has fired. Anything else, including cancellation (also
    while waiting), propagates at once without another attempt. An exhausted budget raises the last error:
    ``HTTP <status>: ...`` for a status, ``Connection failed ...`` for a transport error.
    """
    active = policy or RETRY_POLICY
    number = 1
    while True:
        try:
            return await attempt()
        except RETRYABLE_TRANSPORT_ERRORS as exc:
            if not _can_retry(number, active, delivery):
                after = "" if number == 1 else f" after {number} attempts"
                raise LLMError(f"Connection failed{after}: {exc}") from exc
            reason, retry_after = type(exc).__name__, None
        except LLMHTTPError as exc:
            if exc.status_code not in RETRYABLE_STATUS_CODES or not _can_retry(number, active, delivery):
                raise
            reason, retry_after = f"HTTP {exc.status_code}", exc.retry_after
        delay = active.delay_for(number, retry_after)
        logger.warning(
            "[LLM] {} attempt {}/{} failed ({}); retrying in {:.2f}s",
            provider,
            number,
            active.max_attempts,
            reason,
            delay,
        )
        await active.sleep(delay)
        number += 1
