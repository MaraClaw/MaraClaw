"""Periodic expiry of unpaid capacity orders and monthly renewal of paid extras (``capacity_renewal``)."""

from typing import Final

from anyio import sleep
from psycopg import Error as PsycopgError

from app.config import get_settings
from app.core.logging import logger, new_trace_id
from app.db.errors import DbError
from app.runtime.tasks import capped_backoff, loop_progress
from app.services.capacity_renewals import run_renewal_pass

# A pass is a few set-based statements plus one short transaction per company due a renewal.
PASS_ALLOWANCE_SECONDS: Final = 600.0
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0


def progress_budget_seconds() -> float:
    return get_settings().CAPACITY_RENEWAL_INTERVAL_SECONDS + PASS_ALLOWANCE_SECONDS


async def start_capacity_renewal_worker() -> None:
    """Run a renewal pass, then sleep the interval; beat only after a pass that finished.

    One company's failed renewal is logged inside the pass and the pass continues. A database error that stops the
    whole pass is retried in place with capped backoff and never beats.
    """
    progress = loop_progress()
    failures = 0
    new_trace_id()
    while True:
        try:
            result = await run_renewal_pass()
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("[capacity] renewal pass failed; will retry: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1
            continue
        failures = 0
        if result.expired or result.created or result.failed:
            logger.info(
                "[capacity] renewal pass expired={} created={} failed={}", result.expired, result.created, result.failed
            )
        progress.beat()
        await sleep(get_settings().CAPACITY_RENEWAL_INTERVAL_SECONDS)
        new_trace_id()
