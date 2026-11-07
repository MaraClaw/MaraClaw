"""Classify Tavily upstream failures for rotate-vs-retry, and name the proxy's own local limits."""

from __future__ import annotations

from typing import final

# Tavily's documented key/plan usage limits: 432 (key or plan limit) and 433 (pay-as-you-go limit). A 429 is a
# rate limit that clears on its own, so it is passed through without exhausting the key.
_QUOTA_STATUSES = frozenset({432, 433})


def is_quota_error(status_code: int) -> bool:
    """Return True when the response means this key is exhausted and we should rotate."""
    return status_code in _QUOTA_STATUSES


def is_transient_error(status_code: int) -> bool:
    """Return True for same-key retry (not a reason to burn the next key)."""
    return status_code >= 500 or status_code == 408


class TavilyLimitError(Exception):
    """The proxy gave up on its own limits; the key and the ring are not at fault.

    Deliberately not a ``TavilyProxyError``: the rotation loop must never rotate, retry or exhaust on it.
    """

    status_code: int
    body: str
    bytes_read: int

    def __init__(self, status_code: int, body: str, *, bytes_read: int = 0) -> None:
        super().__init__(body)
        self.status_code = status_code
        self.body = body
        self.bytes_read = bytes_read


@final
class TavilyResponseTooLargeError(TavilyLimitError):
    """The decoded upstream body crossed ``TAVILY_PROXY_MAX_RESPONSE_BYTES``."""

    def __init__(self, *, bytes_read: int) -> None:
        super().__init__(502, '{"detail":"Tavily response exceeded the proxy size limit"}', bytes_read=bytes_read)


@final
class TavilyDeadlineExceededError(TavilyLimitError):
    """The call's single monotonic deadline (``TAVILY_PROXY_DEADLINE_SECONDS``) ran out."""

    def __init__(self, *, bytes_read: int = 0) -> None:
        super().__init__(504, '{"detail":"Tavily proxy deadline exceeded"}', bytes_read=bytes_read)
