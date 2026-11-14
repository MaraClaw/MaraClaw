"""Per-client-IP budget of the public organization admission submit endpoint.

A fixed one-hour window kept in Redis (``INCR`` + ``EXPIRE``). Every submission that reaches the limiter counts, whether
or not it is accepted. When Redis cannot answer the request is allowed: the limiter is a brake, not the authorization.
"""

from datetime import datetime
from typing import Final

from app.core.events import get_redis
from app.core.json_types import int_from_row
from app.core.logging import logger

SUBMISSIONS_PER_WINDOW: Final = 5
WINDOW_SECONDS: Final = 3600
_UNKNOWN_CLIENT: Final = "unknown"


async def allow_submission(client_ip: str | None, *, now: datetime) -> bool:
    window = int(now.timestamp()) // WINDOW_SECONDS
    key = f"organization_admission:submit:{client_ip or _UNKNOWN_CLIENT}:{window}"
    try:
        redis = await get_redis()
        async with redis.pipeline(transaction=True) as pipe:
            _ = pipe.incr(key)
            _ = pipe.expire(key, WINDOW_SECONDS)
            executed = list[object](await pipe.execute())
        hits = int_from_row(executed[0])
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning(
            "[organization-admission] submit rate limiter unavailable; allowing the request"
        )
        return True
    return hits <= SUBMISSIONS_PER_WINDOW
