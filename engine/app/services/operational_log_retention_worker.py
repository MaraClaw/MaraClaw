"""Bounded, periodic deletion of old operational rows: agent activity and audit logs, finished trigger executions,
notifications, done tasks with their logs, channel event receipts, finished gateway report deliveries and superseded
workspace file revisions. Old completed gateway messages lose their content and result but keep the row.

Every table is opt-in through its own retention setting; 0 keeps its rows forever and issues no SQL for it."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from anyio import sleep
from psycopg import Error as PsycopgError

from app.config import Settings, get_settings
from app.core.logging import logger
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.audit_log_dao import audit_log_dao
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.dao.notification_dao import notification_dao
from app.dao.task_dao import task_dao
from app.dao.trigger_dao import trigger_execution_dao
from app.dao.workspace_dao import workspace_file_revision_dao
from app.db.errors import DbError
from app.runtime.tasks import capped_backoff, loop_progress

# One index-ordered statement per table per pass stays far below the pooled statement timeout; full batches run back
# to back.
PURGE_LIMIT: Final = 5000
POLL_SECONDS: Final = 60
# Readiness fails when no pass finished this long; one pass is one bounded batch per table plus at most POLL_SECONDS.
PROGRESS_BUDGET_SECONDS: Final = 300.0
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0


@dataclass(frozen=True, slots=True)
class _Table:
    """One retained table: its window in days (0 is off) and one committed batch given a cutoff and a limit."""

    name: str
    retention_days: Callable[[Settings], int]
    purge: Callable[[datetime, int], Awaitable[int]]


# The DAOs are read when a batch runs, not here, so each purge calls the module's current DAO.
_TABLES: Final = (
    _Table(
        "activity log",
        lambda settings: settings.ACTIVITY_LOG_RETENTION_DAYS,
        lambda cutoff, limit: agent_activity_log_dao.purge_older_than(older_than=cutoff, limit=limit),
    ),
    _Table(
        "trigger execution",
        lambda settings: settings.TRIGGER_EXECUTION_RETENTION_DAYS,
        lambda cutoff, limit: trigger_execution_dao.purge_finished_before(finished_before=cutoff, limit=limit),
    ),
    _Table(
        "gateway message",
        lambda settings: settings.GATEWAY_MESSAGE_RETENTION_DAYS,
        lambda cutoff, limit: gateway_message_dao.reclaim_completed_before(completed_before=cutoff, limit=limit),
    ),
    _Table(
        "notification",
        lambda settings: settings.NOTIFICATION_RETENTION_DAYS,
        lambda cutoff, limit: notification_dao.purge_older_than(older_than=cutoff, limit=limit),
    ),
    _Table(
        "task",
        lambda settings: settings.TASK_RETENTION_DAYS,
        lambda cutoff, limit: task_dao.purge_finished_before(finished_before=cutoff, limit=limit),
    ),
    _Table(
        "channel event receipt",
        lambda settings: settings.CHANNEL_EVENT_RECEIPT_RETENTION_DAYS,
        lambda cutoff, limit: channel_event_receipt_dao.purge_older_than(older_than=cutoff, limit=limit),
    ),
    _Table(
        "gateway report delivery",
        lambda settings: settings.GATEWAY_REPORT_DELIVERY_RETENTION_DAYS,
        lambda cutoff, limit: gateway_report_delivery_dao.purge_finished_before(finished_before=cutoff, limit=limit),
    ),
    _Table(
        "workspace revision",
        lambda settings: settings.WORKSPACE_REVISION_RETENTION_DAYS,
        lambda cutoff, limit: workspace_file_revision_dao.purge_superseded_before(created_before=cutoff, limit=limit),
    ),
    _Table(
        "audit log",
        lambda settings: settings.AUDIT_LOG_RETENTION_DAYS,
        lambda cutoff, limit: audit_log_dao.purge_older_than(older_than=cutoff, limit=limit),
    ),
)


async def start_operational_log_retention_worker() -> None:
    """Purge one batch per enabled table per pass; beat after every pass in which no purge raised.

    Each table's purge is its own transaction, and a retention of 0 for a table makes no database call for it:
    with every table at 0 the pass touches nothing and still beats. A database error on any table retries the pass
    in place with capped backoff and never beats; the other tables are still attempted. The loop sleeps only when
    every batch it ran came back short, so a table with a backlog starts the next pass at once and, since each pass
    takes one batch from every enabled table, never holds the others back. Cancelling mid-pass leaves only batches
    that already committed.
    """
    progress = loop_progress()
    failures = 0
    while True:
        settings = get_settings()
        now = datetime.now(UTC)
        batches: list[int] = []
        failed = False
        for table in _TABLES:
            days = table.retention_days(settings)
            if days <= 0:
                continue
            try:
                batches.append(await table.purge(now - timedelta(days=days), PURGE_LIMIT))
            except (DbError, PsycopgError, OSError) as exc:
                logger.warning("[OperationalLogRetention] {} retention failed: {}", table.name, type(exc).__name__)
                failed = True
        if failed:
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1
            continue
        failures = 0
        progress.beat()
        if all(deleted < PURGE_LIMIT for deleted in batches):
            await sleep(POLL_SECONDS)
