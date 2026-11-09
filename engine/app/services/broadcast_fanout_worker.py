"""Resume committed broadcast ledgers without performing SMTP I/O."""

from typing import Final

from anyio import fail_after, sleep
from psycopg import Error as PsycopgError

from app.core.logging import logger, new_trace_id
from app.dao.broadcast_job_batch import process_broadcast_batch
from app.db.errors import DbError
from app.runtime.tasks import capped_backoff, loop_progress

BATCH_SIZE: Final = 100
POLL_SECONDS: Final = 1
BATCH_TIMEOUT_SECONDS: Final = 30
PROGRESS_BUDGET_SECONDS: Final = 90.0


async def start_broadcast_fanout_worker() -> None:
    """Own one transaction at a time; database failures retain pending work for retry."""
    progress = loop_progress()
    failures = 0
    while True:
        new_trace_id()
        try:
            with fail_after(BATCH_TIMEOUT_SECONDS):
                worked = await process_broadcast_batch(limit=BATCH_SIZE)
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("[BroadcastFanout] batch failed; will retry: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=1.0, cap_seconds=30.0))
            failures += 1
            continue
        failures = 0
        progress.beat()
        if not worked:
            await sleep(POLL_SECONDS)
