"""Per-client-IP budget of the public organization admission submit endpoint.

A fixed one-hour window on the shared limiter (``app.core.rate_limit``). Every submission that reaches the limiter
counts, whether or not it is accepted. When Redis cannot answer the request is allowed: the limiter is a brake, not the
authorization.
"""

from datetime import datetime
from typing import Final

from app.core.rate_limit import Limit, consume

SUBMISSIONS_PER_WINDOW: Final = 5
WINDOW_SECONDS: Final = 3600
_UNKNOWN_CLIENT: Final = "unknown"


async def allow_submission(client_ip: str | None, *, now: datetime) -> bool:
    limit = Limit("organization_admission:submit", client_ip or _UNKNOWN_CLIENT, SUBMISSIONS_PER_WINDOW, WINDOW_SECONDS)
    return await consume(limit, now=now) is None
