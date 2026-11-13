"""Bounded, periodic deletion of old agent activity logs and finished trigger executions."""

from datetime import UTC, datetime, timedelta
from typing import Final

from anyio import sleep
from psycopg import Error as PsycopgError

from app.config import get_settings
from app.core.logging import logger
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.trigger_dao import trigger_execution_dao
from app.db.errors import DbError
from app.runtime.tasks import capped_backoff, loop_progress

# One index-ordered DELETE per table per pass stays far below the pooled statement timeout; full batches run back to back.
PURGE_LIMIT: Final = 5000
POLL_SECONDS: Final = 60
# Readiness fails when no pass finished this long; one pass is two batches plus at most POLL_SECONDS.
PROGRESS_BUDGET_SECONDS: Final = 300.0
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0


async def start_operational_log_retention_worker() -> None:
    """Purge one batch per enabled table per pass; beat after every pass in which no purge raised.

    Each table's purge is its own transaction, and a retention of 0 for a table makes no database call for it:
    with both at 0 the pass touches nothing and still beats. A database error on either table retries the pass in
    place with capped backoff and never beats. The loop sleeps only when every batch it ran came back short.
    """
    progress = loop_progress()
    failures = 0
    while True:
        settings = get_settings()
        now = datetime.now(UTC)
        batches: list[int] = []
        failed = False
        if settings.ACTIVITY_LOG_RETENTION_DAYS > 0:
            cutoff = now - timedelta(days=settings.ACTIVITY_LOG_RETENTION_DAYS)
            try:
                batches.append(await agent_activity_log_dao.purge_older_than(older_than=cutoff, limit=PURGE_LIMIT))
            except (DbError, PsycopgError, OSError) as exc:
                logger.warning("[OperationalLogRetention] activity log purge failed: {}", type(exc).__name__)
                failed = True
        if settings.TRIGGER_EXECUTION_RETENTION_DAYS > 0:
            cutoff = now - timedelta(days=settings.TRIGGER_EXECUTION_RETENTION_DAYS)
            try:
                batches.append(
                    await trigger_execution_dao.purge_finished_before(finished_before=cutoff, limit=PURGE_LIMIT)
                )
            except (DbError, PsycopgError, OSError) as exc:
                logger.warning("[OperationalLogRetention] trigger execution purge failed: {}", type(exc).__name__)
                failed = True
        if failed:
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1
            continue
        failures = 0
        progress.beat()
        if all(deleted < PURGE_LIMIT for deleted in batches):
            await sleep(POLL_SECONDS)
