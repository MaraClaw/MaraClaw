"""Classify Linkup upstream failures for rotate-vs-retry."""

from __future__ import annotations

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
