"""Unified LLM failover error classification.

Provides error classification for failover decisions across all execution paths.
"""

from __future__ import annotations

from enum import Enum

import httpx

from app.services.llm.retry_policy import LLMHTTPError

RATE_LIMITED_STATUS = 429
SERVER_ERROR_STATUSES = range(500, 600)


class FailoverErrorType(Enum):
    """Classification of LLM errors for failover decisions."""

    RETRYABLE = "retryable"  # Network timeout, 429, 5xx, transient errors
    NON_RETRYABLE = "non_retryable"  # Auth, validation, schema errors
    UNKNOWN = "unknown"


def classify_error(error: Exception) -> FailoverErrorType:
    """Classify an exception as retryable or non-retryable.

    A provider's HTTP status decides first: 429 and 5xx are retryable, every other status is the caller's mistake
    and never retryable, whatever its body says. Errors without a status are classified by their message, and a
    typed timeout whose message names nothing is retryable.

    Retryable errors:
    - Network timeout / connection errors
    - Provider 429 (rate limit)
    - Provider 5xx (server errors)
    - Explicit transient provider errors

    Non-retryable errors:
    - Auth errors (401, 403)
    - Validation errors (400, 422)
    - Schema errors
    - Content policy violations
    """
    if isinstance(error, LLMHTTPError):
        return _classify_status(error.status_code)
    kind = _classify_message(str(error).lower())
    if kind is FailoverErrorType.UNKNOWN and isinstance(error, httpx.TimeoutException | TimeoutError):
        return FailoverErrorType.RETRYABLE
    return kind


def _classify_status(status_code: int) -> FailoverErrorType:
    if status_code == RATE_LIMITED_STATUS or status_code in SERVER_ERROR_STATUSES:
        return FailoverErrorType.RETRYABLE
    return FailoverErrorType.NON_RETRYABLE


def _classify_message(error_msg: str) -> FailoverErrorType:
    # Non-retryable: authentication and authorization
    if any(kw in error_msg for kw in ["auth", "unauthorized", "forbidden", "invalid api key", "api key invalid"]):
        return FailoverErrorType.NON_RETRYABLE

    # Non-retryable: validation and schema
    if any(kw in error_msg for kw in ["validation", "invalid request", "schema", "bad request"]):
        return FailoverErrorType.NON_RETRYABLE

    # Non-retryable: content policy
    if any(kw in error_msg for kw in ["content policy", "content_filter", "safety", "moderation"]):
        return FailoverErrorType.NON_RETRYABLE

    # Retryable: rate limiting
    if any(kw in error_msg for kw in ["rate limit", "429", "too many requests"]):
        return FailoverErrorType.RETRYABLE

    # Retryable: server errors
    if any(kw in error_msg for kw in ["500", "502", "503", "504", "server error", "internal error"]):
        return FailoverErrorType.RETRYABLE

    # Network failures and timeouts can be retried.
    if any(kw in error_msg for kw in ["timeout", "connection", "network", "unreachable", "refused", "reset", "dns"]):
        return FailoverErrorType.RETRYABLE

    # Retryable: transient errors
    if any(kw in error_msg for kw in ["temporary", "transient", "unavailable", "overloaded", "busy"]):
        return FailoverErrorType.RETRYABLE

    # Check the error message for HTTP status codes
    if any(code in error_msg for code in ["401", "403", "400", "422"]):
        return FailoverErrorType.NON_RETRYABLE
    if any(code in error_msg for code in ["429", "500", "502", "503", "504", "408"]):
        return FailoverErrorType.RETRYABLE

    # If it's an error result string, it's likely retryable by default
    if error_msg.startswith(("[llm error]", "[llm call error]", "[error]")):
        return FailoverErrorType.RETRYABLE

    return FailoverErrorType.UNKNOWN


__all__ = [
    "FailoverErrorType",
    "classify_error",
]
