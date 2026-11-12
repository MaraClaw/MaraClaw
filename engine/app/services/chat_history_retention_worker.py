"""Bounded, periodic deletion of chat history journal rows older than the retention window."""

from datetime import UTC, datetime, timedelta
from typing import Final

from anyio import sleep
from psycopg import Error as PsycopgError

from app.config import get_settings
from app.core.logging import logger
from app.dao.chat_dao import chat_message_dao
from app.db.errors import DbError
from app.runtime.tasks import capped_backoff, loop_progress

# One index-ordered DELETE per batch stays far below the pooled statement timeout; full batches run back to back.
PURGE_LIMIT: Final = 5000
POLL_SECONDS: Final = 60
# Readiness fails when no batch finished this long; one pass is one batch plus at most POLL_SECONDS.
PROGRESS_BUDGET_SECONDS: Final = 300.0
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0


async def start_chat_history_retention_worker() -> None:
    """Purge one batch per pass; beat after every completed batch so a long backlog drain stays ready.

    ``CHAT_HISTORY_RETENTION_DAYS = 0`` keeps every row: the pass makes no database call and still beats.
    Database errors are retried in place with capped backoff and never beat.
    """
    progress = loop_progress()
    failures = 0
    while True:
        days = get_settings().CHAT_HISTORY_RETENTION_DAYS
        deleted = 0
        if days > 0:
            try:
                deleted = await chat_message_dao.purge_history_changes(
                    older_than=datetime.now(UTC) - timedelta(days=days), limit=PURGE_LIMIT
                )
            except (DbError, PsycopgError, OSError) as exc:
                logger.warning("[ChatHistoryRetention] purge failed; will retry: {}", type(exc).__name__)
                await sleep(
                    capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS)
                )
                failures += 1
                continue
            failures = 0
        progress.beat()
        if deleted < PURGE_LIMIT:
            await sleep(POLL_SECONDS)
