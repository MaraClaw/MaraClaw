"""Shared fixed-window limiter on Redis for public endpoints.

Every call counts one attempt (``INCR`` + ``EXPIRE`` in one transaction), whether or not the request is later accepted.
A Redis error allows the request: the limiter is a brake, not the authorization, and the bounded bcrypt queue in
``security.py`` is the backstop. Callers throttle before any password check or email send.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from fastapi import HTTPException, status

from app.core.events import get_redis
from app.core.json_types import int_from_row
from app.core.logging import logger
from app.core.metrics import Family, counter_family

TOO_MANY_REQUESTS_DETAIL: Final = "Too many requests. Try again later."
# The metric label comes from this closed set; a scope not listed here is counted as ``other``. Add each new scope here.
REGISTERED_SCOPES: Final = frozenset(
    {
        "auth:login:address",
        "auth:forgot-password:address",
        "auth:resend-verification:address",
        "auth:ticket:address",
        "auth:login:account",
        "auth:forgot-password:account",
        "auth:resend-verification:account",
        "auth:ticket:user",
        "organization_admission:submit",
    }
)
_OTHER_SCOPE: Final = "other"
_fail_open_allows = dict.fromkeys((*sorted(REGISTERED_SCOPES), _OTHER_SCOPE), 0)


@dataclass(frozen=True, slots=True)
class Limit:
    """One budget: ``attempts`` per ``window_seconds`` for ``subject`` under ``scope``."""

    scope: str
    subject: str
    attempts: int
    window_seconds: int


def _now() -> datetime:
    return datetime.now(UTC)


def too_many_requests(retry_after: int) -> HTTPException:
    """The one refusal every limit answers with, so status, body and headers never depend on who was throttled."""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=TOO_MANY_REQUESTS_DETAIL,
        headers={"Retry-After": str(retry_after)},
    )


def _count_fail_open(scope: str) -> None:
    _fail_open_allows[scope if scope in REGISTERED_SCOPES else _OTHER_SCOPE] += 1


def metric_families() -> tuple[Family, ...]:
    """Requests allowed because the limiter's Redis was unavailable; read at scrape time, no I/O."""
    return (
        counter_family(
            "maraclaw_rate_limit_fail_open",
            "Requests a rate limiter allowed because Redis was unavailable, by limiter scope.",
            (((("scope", scope),), count) for scope, count in _fail_open_allows.items()),
        ),
    )


async def consume(limit: Limit, *, now: datetime | None = None) -> int | None:
    """Count one attempt; return the seconds until the window renews when the budget is spent, else ``None``."""
    window, elapsed = divmod(int((now or _now()).timestamp()), limit.window_seconds)
    key = f"{limit.scope}:{limit.subject}:{window}"
    try:
        redis = await get_redis()
        async with redis.pipeline(transaction=True) as pipe:
            _ = pipe.incr(key)
            _ = pipe.expire(key, limit.window_seconds)
            executed = list[object](await pipe.execute())
        hits = int_from_row(executed[0])
    except Exception as exc:
        _count_fail_open(limit.scope)
        logger.bind(exception_type=type(exc).__name__).warning(
            "[rate-limit] {} limiter unavailable; allowing the request", limit.scope
        )
        return None
    if hits <= limit.attempts:
        return None
    return limit.window_seconds - elapsed


async def enforce(*limits: Limit) -> None:
    """Consume the limits in order and raise the shared 429 at the first spent budget; later ones are not counted."""
    for limit in limits:
        retry_after = await consume(limit)
        if retry_after is not None:
            raise too_many_requests(retry_after)
