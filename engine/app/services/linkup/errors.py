"""Classify Linkup upstream failures for rotate-vs-retry, and name the proxy's own local limits."""

from __future__ import annotations

from typing import final

_QUOTA_MARKERS = ("quota", "insufficient", "credit", "payment", "plan", "billing")


def is_quota_error(status_code: int, body: str) -> bool:
    """Return True when the response means this key is exhausted and we should rotate."""
    if status_code == 402:
        return True
    if status_code != 429:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)


def is_transient_error(status_code: int) -> bool:
    """Return True for same-key retry (not a reason to burn the next key)."""
    return status_code >= 500 or status_code == 408


class LinkupLimitError(Exception):
    """The proxy gave up on its own limits; the key and the ring are not at fault.

    Deliberately not a ``LinkupProxyError``: the rotation loop must never rotate, retry or exhaust on it.
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
class LinkupResponseTooLargeError(LinkupLimitError):
    """The decoded upstream body crossed ``LINKUP_PROXY_MAX_RESPONSE_BYTES``."""

    def __init__(self, *, bytes_read: int) -> None:
        super().__init__(502, '{"detail":"Linkup response exceeded the proxy size limit"}', bytes_read=bytes_read)


@final
class LinkupDeadlineExceededError(LinkupLimitError):
    """The call's single monotonic deadline (``LINKUP_PROXY_DEADLINE_SECONDS``) ran out."""

    def __init__(self, *, bytes_read: int = 0) -> None:
        super().__init__(504, '{"detail":"Linkup proxy deadline exceeded"}', bytes_read=bytes_read)
