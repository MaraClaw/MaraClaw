"""Real PostgreSQL: status filtering, batch bounds, lock skipping and index use of the retention purges.

Agent activity logs, trigger executions, notifications, done tasks with their logs, channel event receipts (with the
media descriptors and burst outcomes that cascade from them), delivered or terminal gateway report deliveries and
superseded workspace file revisions are deleted; completed gateway messages keep their row and lose only their
content and result, so delayed-reply classification and pending relays still find them.

Opt in with MARACLAW_TEST_POSTGRES_DSN; each test gets a unique schema built from the baseline, dropped afterwards.
Lock tests never sleep: a purge that waited on a locked row would hit the transaction's lock_timeout (or the
fail_after bound) and fail, instead of passing by timing luck.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import anyio
import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.config import MIN_CHANNEL_EVENT_RECEIPT_RETENTION_DAYS, MIN_GATEWAY_MESSAGE_RETENTION_DAYS, get_settings
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.dao.notification_dao import notification_dao
from app.dao.openclaw_reply_dao import openclaw_reply_dao
from app.dao.task_dao import task_dao
from app.dao.trigger_dao import trigger_execution_dao
from app.dao.workspace_dao import workspace_file_revision_dao
from app.db.connection import DbConnection, Params, Row
from app.db.session import transaction
from app.scripts import bootstrap_db
from app.services import operational_log_retention_worker as worker

CUTOFF = datetime(2026, 11, 1, tzinfo=UTC)
OLD = CUTOFF - timedelta(days=3)
RECENT = CUTOFF + timedelta(days=3)
# A purge that waits on a locked row fails after this long instead of hanging the run.
GUARD = "SET LOCAL lock_timeout = '1s'"
FAIL_AFTER_SECONDS = 10

type Purge = Callable[[DbConnection, int], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class RetentionDatabase:
    first: DbConnection
    second: DbConnection
    agent_id: UUID
    trigger_id: UUID
    user_id: UUID
    config_id: UUID


@pytest.fixture
async def retention_db() -> AsyncIterator[RetentionDatabase]:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("MARACLAW_TEST_POSTGRES_DSN is required for isolated PostgreSQL retention QA")
    schema = f"op_log_retention_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(dsn, autocommit=True, row_factory=dict_row) as admin:
        owner = DbConnection(admin)
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        try:
            isolated = make_conninfo(dsn, options=f"-csearch_path={schema}")
            async with (
                await AsyncConnection[DictRow].connect(isolated, row_factory=dict_row) as first,
                await AsyncConnection[DictRow].connect(isolated, row_factory=dict_row) as second,
            ):
                db = DbConnection(first)
                user_id, agent_id, trigger_id, config_id = uuid4(), uuid4(), uuid4(), uuid4()
                async with transaction(db):
                    statements = bootstrap_db._statement_bodies(bootstrap_db.SCHEMA_BASELINE.read_text())
                    # The baseline is rerunnable, so the second pass proves the new index is idempotent.
                    for _ in range(2):
                        for statement in statements:
                            await db.execute(statement)
                    await db.execute(
                        "INSERT INTO users (id, display_name, quota_message_limit) VALUES (%(id)s, 'Retention QA', 1)",
                        {"id": user_id},
                    )
                    await db.execute(
                        "INSERT INTO agents (id, name, creator_id, role_description, max_triggers, "
                        "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
                        "heartbeat_interval_minutes) VALUES (%(id)s, 'Retention QA', %(user)s, '', 20, 5, 5, "
                        "'private', 'use', 240)",
                        {"id": agent_id, "user": user_id},
                    )
                    await add_trigger(db, agent_id, trigger_id)
                    await db.execute(
                        "INSERT INTO channel_configs (id, agent_id, channel_type, app_id, is_configured, is_connected) "
                        "VALUES (%(id)s, %(agent)s, 'feishu', 'app', true, true)",
                        {"id": config_id, "agent": agent_id},
                    )
                yield RetentionDatabase(db, DbConnection(second), agent_id, trigger_id, user_id, config_id)
        finally:
            await owner.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def add_trigger(db: DbConnection, agent_id: UUID, trigger_id: UUID) -> None:
    await db.execute(
        "INSERT INTO agent_triggers (id, agent_id, name, type, config, reason, fire_count, max_fires, "
        "cooldown_seconds) VALUES (%(id)s, %(agent)s, %(name)s, 'cron', '{}'::jsonb, 'qa', 5, 10, 0)",
        {"id": trigger_id, "agent": agent_id, "name": f"trigger-{trigger_id}"},
    )


async def add_execution(
    h: RetentionDatabase,
    *,
    status: str,
    finished_at: datetime | None,
    trigger_id: UUID | None = None,
    leased_until: datetime | None = None,
) -> UUID:
    execution_id = uuid4()
    await h.first.execute(
        "INSERT INTO trigger_executions (id, trigger_id, agent_id, source, status, idempotency_key, payload, "
        "payload_text, lease_owner, lease_expires_at, scheduled_at, started_at, finished_at) "
        "VALUES (%(id)s, %(trigger)s, %(agent)s, 'cron', %(status)s, %(key)s, '{}'::jsonb, '', %(owner)s, "
        "%(leased_until)s, %(at)s, %(at)s, %(finished_at)s)",
        {
            "id": execution_id,
            "trigger": trigger_id or h.trigger_id,
            "agent": h.agent_id,
            "status": status,
            "key": f"key-{execution_id}",
            "owner": "worker" if leased_until else None,
            "leased_until": leased_until,
            "at": OLD,
            "finished_at": finished_at,
        },
    )
    return execution_id


async def add_activity(h: RetentionDatabase, created_at: datetime) -> UUID:
    log_id = uuid4()
    await h.first.execute(
        "INSERT INTO agent_activity_logs (id, agent_id, action_type, summary, created_at) "
        "VALUES (%(id)s, %(agent)s, 'chat_reply', 'summary', %(at)s)",
        {"id": log_id, "agent": h.agent_id, "at": created_at},
    )
    return log_id


async def add_gateway(
    h: RetentionDatabase, *, status: str, completed_at: datetime | None, result: str | None = "answer"
) -> UUID:
    message_id = uuid4()
    await h.first.execute(
        "INSERT INTO gateway_messages (id, agent_id, content, status, result, guest_model_ref, created_at, "
        "completed_at) VALUES (%(id)s, %(agent)s, 'question', %(status)s, %(result)s, 'provider/model', %(at)s, "
        "%(completed_at)s)",
        {
            "id": message_id,
            "agent": h.agent_id,
            "status": status,
            "result": result,
            "at": OLD - timedelta(days=1),
            "completed_at": completed_at,
        },
    )
    return message_id


async def add_delivery(
    h: RetentionDatabase, message_id: UUID, *, delivered: bool, terminal: bool, at: datetime = OLD
) -> UUID:
    delivery_id = uuid4()
    await h.first.execute(
        "INSERT INTO gateway_report_deliveries (id, message_id, destination_kind, destination_id, available_at, "
        "delivered_at, terminal_reason, terminal_at) VALUES (%(id)s, %(message)s, 'agent', %(agent)s, %(at)s, "
        "%(delivered_at)s, %(reason)s, %(terminal_at)s)",
        {
            "id": delivery_id,
            "message": message_id,
            "agent": h.agent_id,
            "at": OLD,
            "delivered_at": at if delivered else None,
            "reason": "payload_rejected" if terminal else None,
            "terminal_at": at if terminal else None,
        },
    )
    return delivery_id


async def add_notification(h: RetentionDatabase, created_at: datetime, *, is_read: bool = False) -> UUID:
    notification_id = uuid4()
    await h.first.execute(
        "INSERT INTO notifications (id, user_id, type, title, body, is_read, created_at) "
        "VALUES (%(id)s, %(user)s, 'system', 'title', 'body', %(is_read)s, %(at)s)",
        {"id": notification_id, "user": h.user_id, "is_read": is_read, "at": created_at},
    )
    return notification_id


async def add_task(
    h: RetentionDatabase,
    *,
    status: str,
    completed_at: datetime | None,
    updated_at: datetime | None = None,
    logs: int = 2,
) -> UUID:
    task_id = uuid4()
    finished = updated_at or completed_at or OLD
    await h.first.execute(
        "INSERT INTO tasks (id, agent_id, title, type, status, priority, assignee, created_by, created_at, "
        "updated_at, completed_at) VALUES (%(id)s, %(agent)s, 'task', 'todo', %(status)s, 'low', 'agent', %(user)s, "
        "%(at)s, %(updated_at)s, %(completed_at)s)",
        {
            "id": task_id,
            "agent": h.agent_id,
            "user": h.user_id,
            "status": status,
            "at": OLD - timedelta(days=1),
            "updated_at": finished,
            "completed_at": completed_at,
        },
    )
    for _ in range(logs):
        await h.first.execute(
            "INSERT INTO task_logs (id, task_id, content, created_at) VALUES (%(id)s, %(task)s, 'log', %(at)s)",
            {"id": uuid4(), "task": task_id, "at": OLD},
        )
    return task_id


async def add_receipt(h: RetentionDatabase, created_at: datetime, *, dependents: bool = False) -> UUID:
    """Insert a receipt whose event key is a UUID; ``dependents`` adds a media descriptor and a burst outcome."""
    receipt_id = uuid4()
    key = {"config_id": h.config_id, "key": str(receipt_id)}
    await h.first.execute(
        "INSERT INTO channel_event_receipts (config_id, provider, event_key, agent_id, fingerprint, created_at) "
        "VALUES (%(config_id)s, 'feishu', %(key)s, %(agent)s, %(fingerprint)s, %(at)s)",
        {**key, "agent": h.agent_id, "fingerprint": "v1:" + "a" * 64, "at": created_at},
    )
    if dependents:
        await h.first.execute(
            "INSERT INTO channel_event_media (config_id, provider, event_key, ordinal, gateway_message_id, stored_file) "
            "VALUES (%(config_id)s, 'feishu', %(key)s, 0, %(message)s, '{}'::jsonb)",
            {**key, "message": uuid4()},
        )
        await h.first.execute(
            "INSERT INTO personal_burst_inputs (id, config_id, provider, event_key, outcome) "
            "VALUES (%(id)s, %(config_id)s, 'feishu', %(key)s, 'capacity_rejected')",
            {**key, "id": uuid4()},
        )
    return receipt_id


async def add_revision(h: RetentionDatabase, path: str, created_at: datetime, *, agent_id: UUID | None = None) -> UUID:
    revision_id = uuid4()
    await h.first.execute(
        "INSERT INTO workspace_file_revisions (id, agent_id, path, operation, actor_type, content_hash, created_at, "
        "updated_at) VALUES (%(id)s, %(agent)s, %(path)s, 'write', 'user', 'hash', %(at)s, %(at)s)",
        {"id": revision_id, "agent": agent_id or h.agent_id, "path": path, "at": created_at},
    )
    return revision_id


async def add_agent(h: RetentionDatabase) -> UUID:
    agent_id = uuid4()
    await h.first.execute(
        "INSERT INTO agents (id, name, creator_id, role_description, max_triggers, min_poll_interval_min, "
        "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) VALUES (%(id)s, "
        "'Second QA', %(user)s, '', 20, 5, 5, 'private', 'use', 240)",
        {"id": agent_id, "user": h.user_id},
    )
    return agent_id


async def purge_notifications(db: DbConnection, limit: int) -> int:
    return await notification_dao.purge_older_than(older_than=CUTOFF, limit=limit)


async def purge_tasks(db: DbConnection, limit: int) -> int:
    return await task_dao.purge_finished_before(finished_before=CUTOFF, limit=limit)


async def purge_receipts(db: DbConnection, limit: int) -> int:
    return await channel_event_receipt_dao.purge_older_than(older_than=CUTOFF, limit=limit)


async def purge_deliveries(db: DbConnection, limit: int) -> int:
    return await gateway_report_delivery_dao.purge_finished_before(finished_before=CUTOFF, limit=limit)


async def purge_revisions(db: DbConnection, limit: int) -> int:
    return await workspace_file_revision_dao.purge_superseded_before(created_before=CUTOFF, limit=limit)


async def purge_activity(db: DbConnection, limit: int) -> int:
    return await agent_activity_log_dao.purge_older_than(older_than=CUTOFF, limit=limit)


async def purge_executions(db: DbConnection, limit: int) -> int:
    return await trigger_execution_dao.purge_finished_before(finished_before=CUTOFF, limit=limit)


async def reclaim_gateway(db: DbConnection, limit: int) -> int:
    return await gateway_message_dao.reclaim_completed_before(completed_before=CUTOFF, limit=limit)


async def seed_expired_activity(h: RetentionDatabase, offset_minutes: int) -> UUID:
    return await add_activity(h, OLD + timedelta(minutes=offset_minutes))


async def seed_expired_execution(h: RetentionDatabase, offset_minutes: int) -> UUID:
    return await add_execution(h, status="completed", finished_at=OLD + timedelta(minutes=offset_minutes))


async def seed_expired_gateway(h: RetentionDatabase, offset_minutes: int) -> UUID:
    return await add_gateway(h, status="completed", completed_at=OLD + timedelta(minutes=offset_minutes))


async def seed_expired_notification(h: RetentionDatabase, offset_minutes: int) -> UUID:
    return await add_notification(h, OLD + timedelta(minutes=offset_minutes))


async def seed_expired_task(h: RetentionDatabase, offset_minutes: int) -> UUID:
    return await add_task(h, status="done", completed_at=OLD + timedelta(minutes=offset_minutes))


async def seed_expired_receipt(h: RetentionDatabase, offset_minutes: int) -> UUID:
    return await add_receipt(h, OLD + timedelta(minutes=offset_minutes))


async def seed_expired_delivery(h: RetentionDatabase, offset_minutes: int) -> UUID:
    message_id = await add_gateway(h, status="completed", completed_at=OLD)
    return await add_delivery(h, message_id, delivered=True, terminal=False, at=OLD + timedelta(minutes=offset_minutes))


async def seed_expired_revision(h: RetentionDatabase, offset_minutes: int) -> UUID:
    """A superseded revision; its newer revision sits after the cutoff and so is never counted as remaining."""
    path = f"notes/{uuid4()}.md"
    revision_id = await add_revision(h, path, OLD + timedelta(minutes=offset_minutes))
    _ = await add_revision(h, path, RECENT)
    return revision_id


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    purge: Purge
    seed_expired: Callable[[RetentionDatabase, int], Awaitable[UUID]]
    select_ids: str
    index: str


TABLES = {
    "agent_activity_logs": Table(
        "agent_activity_logs",
        purge_activity,
        seed_expired_activity,
        "SELECT id FROM agent_activity_logs",
        "ix_agent_activity_logs_created_at",
    ),
    "trigger_executions": Table(
        "trigger_executions",
        purge_executions,
        seed_expired_execution,
        "SELECT id FROM trigger_executions",
        "ix_trigger_executions_finished_at",
    ),
    # A reclaimed message stays; only one still holding its payload counts as remaining.
    "gateway_messages": Table(
        "gateway_messages",
        reclaim_gateway,
        seed_expired_gateway,
        "SELECT id FROM gateway_messages WHERE result IS NOT NULL",
        "ix_gateway_messages_completed_at",
    ),
    "notifications": Table(
        "notifications",
        purge_notifications,
        seed_expired_notification,
        "SELECT id FROM notifications",
        "ix_notifications_created_at",
    ),
    "tasks": Table("tasks", purge_tasks, seed_expired_task, "SELECT id FROM tasks", "ix_tasks_done_finished_at"),
    "channel_event_receipts": Table(
        "channel_event_receipts",
        purge_receipts,
        seed_expired_receipt,
        "SELECT event_key::uuid AS id FROM channel_event_receipts",
        "ix_channel_event_receipts_created_at",
    ),
    "gateway_report_deliveries": Table(
        "gateway_report_deliveries",
        purge_deliveries,
        seed_expired_delivery,
        "SELECT id FROM gateway_report_deliveries",
        "ix_gateway_report_deliveries_finished_at",
    ),
    # The newer revision each expired one is judged by is after the cutoff, so only expired rows count as remaining.
    "workspace_file_revisions": Table(
        "workspace_file_revisions",
        purge_revisions,
        seed_expired_revision,
        "SELECT id FROM workspace_file_revisions WHERE created_at < '2026-11-01T00:00:00+00'",
        "ix_workspace_file_revisions_file_created",
    ),
}
NEW_TABLES = [
    "notifications",
    "tasks",
    "channel_event_receipts",
    "gateway_report_deliveries",
    "workspace_file_revisions",
]


async def purge_once(table: Table, db: DbConnection, limit: int) -> int:
    """Run one purge in its own committed transaction under the no-waiting guard."""
    async with transaction(db):
        await db.execute(GUARD)
        with anyio.fail_after(FAIL_AFTER_SECONDS):
            return await table.purge(db, limit)


async def remaining(h: RetentionDatabase, table: Table) -> set[UUID]:
    async with transaction(h.first):
        rows = await h.first.fetchall(table.select_ids)
    return {row["id"] for row in rows if isinstance(row["id"], UUID)}


async def test_baseline_creates_the_cutoff_indexes_idempotently(retention_db: RetentionDatabase) -> None:
    # Given the baseline applied twice by the fixture.
    h = retention_db
    async with transaction(h.first):
        rows = await h.first.fetchall(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname IN ('ix_trigger_executions_finished_at', 'ix_agent_activity_logs_created_at', "
            "'ix_gateway_messages_completed_at')"
        )
    # Then each cutoff index exists once, the execution one partial on the terminal statuses and the gateway one on
    # completed rows that still hold a result.
    definitions = {str(row["indexname"]): str(row["indexdef"]) for row in rows}
    assert set(definitions) == {
        "ix_trigger_executions_finished_at",
        "ix_agent_activity_logs_created_at",
        "ix_gateway_messages_completed_at",
    }
    finished = definitions["ix_trigger_executions_finished_at"]
    assert "(finished_at)" in finished
    assert "completed" in finished
    assert "failed" in finished
    assert "(created_at)" in definitions["ix_agent_activity_logs_created_at"]
    reclaimable = definitions["ix_gateway_messages_completed_at"]
    assert "(completed_at)" in reclaimable
    assert "'completed'" in reclaimable
    assert "result IS NOT NULL" in reclaimable


async def test_execution_purge_deletes_only_expired_terminal_rows_and_leaves_the_rest_intact(
    retention_db: RetentionDatabase,
) -> None:
    # Given expired completed and failed rows, and rows that must survive however old they are.
    h = retention_db
    async with transaction(h.first):
        purged = {
            await add_execution(h, status="completed", finished_at=OLD),
            await add_execution(h, status="failed", finished_at=OLD - timedelta(days=1)),
        }
        kept = {
            await add_execution(h, status="completed", finished_at=CUTOFF),
            await add_execution(h, status="completed", finished_at=RECENT),
            await add_execution(h, status="failed", finished_at=RECENT),
            await add_execution(h, status="completed", finished_at=None),
            await add_execution(h, status="failed", finished_at=None),
            await add_execution(h, status="pending", finished_at=None),
            await add_execution(h, status="pending", finished_at=OLD),
            await add_execution(h, status="processing", finished_at=None, leased_until=OLD),
            await add_execution(h, status="processing", finished_at=OLD, leased_until=OLD),
        }
        triggers_before = await h.first.fetchall("SELECT id, fire_count, max_fires, is_enabled FROM agent_triggers")
    # When every expired row fits in one batch.
    deleted = await purge_once(TABLES["trigger_executions"], h.first, 100)
    # Then exactly the expired terminal rows went, the trigger counters are untouched, and history lists the rest.
    assert deleted == len(purged)
    assert await remaining(h, TABLES["trigger_executions"]) == kept
    async with transaction(h.first):
        triggers_after = await h.first.fetchall("SELECT id, fire_count, max_fires, is_enabled FROM agent_triggers")
        history = await trigger_execution_dao.list_history_for_trigger(h.agent_id, h.trigger_id, limit=100)
    assert triggers_after == triggers_before
    assert triggers_after[0]["fire_count"] == 5
    assert triggers_after[0]["max_fires"] == 10
    assert {row.id for row in history} == kept


async def test_execution_purge_batches_are_bounded_and_oldest_first(retention_db: RetentionDatabase) -> None:
    # Given five expired executions with distinct finish times.
    h = retention_db
    async with transaction(h.first):
        ids = [await seed_expired_execution(h, minutes) for minutes in range(5)]
    table = TABLES["trigger_executions"]
    # When batches of two are purged until one comes back short.
    first = await purge_once(table, h.first, 2)
    left_after_first = await remaining(h, TABLES["trigger_executions"])
    second = await purge_once(table, h.first, 2)
    third = await purge_once(table, h.first, 2)
    fourth = await purge_once(table, h.first, 2)
    # Then no statement exceeded the limit, and the first one took the two oldest.
    assert (first, second, third, fourth) == (2, 2, 1, 0)
    assert left_after_first == set(ids[2:])
    assert await remaining(h, TABLES["trigger_executions"]) == set()


async def test_activity_purge_deletes_only_rows_older_than_the_cutoff_in_bounded_batches(
    retention_db: RetentionDatabase,
) -> None:
    # Given three expired rows, one exactly at the cutoff and one recent.
    h = retention_db
    async with transaction(h.first):
        ids = [await add_activity(h, OLD + timedelta(minutes=minutes)) for minutes in range(3)]
        boundary = await add_activity(h, CUTOFF)
        recent = await add_activity(h, RECENT)
    table = TABLES["agent_activity_logs"]
    # When batches of two are purged until one comes back short.
    first = await purge_once(table, h.first, 2)
    left_after_first = await remaining(h, TABLES["agent_activity_logs"])
    second = await purge_once(table, h.first, 2)
    third = await purge_once(table, h.first, 2)
    # Then each statement stayed within the limit, the oldest went first, and only rows strictly older than the
    # cutoff were deleted.
    assert (first, second, third) == (2, 1, 0)
    assert left_after_first == {ids[2], boundary, recent}
    assert await remaining(h, TABLES["agent_activity_logs"]) == {boundary, recent}


async def test_activity_purge_skips_a_row_another_transaction_holds_without_waiting(
    retention_db: RetentionDatabase,
) -> None:
    # Given three expired rows, one of them locked by another open transaction.
    h = retention_db
    async with transaction(h.first):
        ids = [await add_activity(h, OLD + timedelta(minutes=minutes)) for minutes in range(3)]
    await h.second.fetchall("SELECT id FROM agent_activity_logs WHERE id = %(id)s FOR UPDATE", {"id": ids[0]})
    # When a purge runs meanwhile.
    skipped = await purge_once(TABLES["agent_activity_logs"], h.first, 10)
    held = await remaining(h, TABLES["agent_activity_logs"])
    await h.second.rollback()
    after_release = await purge_once(TABLES["agent_activity_logs"], h.first, 10)
    # Then it completed without waiting, left the held row alone, and took it once the lock was gone.
    assert (skipped, held, after_release) == (2, {ids[0]}, 1)
    assert await remaining(h, TABLES["agent_activity_logs"]) == set()


async def test_execution_purge_skips_rows_a_cascading_trigger_delete_holds_without_waiting(
    retention_db: RetentionDatabase,
) -> None:
    # Given expired executions on two triggers, and an open transaction deleting the first trigger.
    h = retention_db
    other_trigger = uuid4()
    async with transaction(h.first):
        await add_trigger(h.first, h.agent_id, other_trigger)
        held_ids = {await seed_expired_execution(h, minutes) for minutes in range(2)}
        free_ids = {
            await add_execution(h, status="failed", finished_at=OLD, trigger_id=other_trigger) for _ in range(3)
        }
    await h.second.execute("DELETE FROM agent_triggers WHERE id = %(id)s", {"id": h.trigger_id})
    # When a purge runs meanwhile.
    skipped = await purge_once(TABLES["trigger_executions"], h.first, 10)
    held = await remaining(h, TABLES["trigger_executions"])
    await h.second.rollback()
    after_release = await purge_once(TABLES["trigger_executions"], h.first, 10)
    # Then it completed without waiting, took only the other trigger's rows, and the rest once the delete was gone.
    assert (skipped, held, after_release) == (len(free_ids), held_ids, len(held_ids))
    assert await remaining(h, TABLES["trigger_executions"]) == set()


async def test_gateway_reclaim_clears_only_expired_completed_payloads_and_keeps_every_row(
    retention_db: RetentionDatabase,
) -> None:
    # Given expired completed messages whose relays are done, and messages that must keep their payload: one with a
    # relay still pending, completed ones at or after the cutoff or never stamped, and unfinished ones however old.
    h = retention_db
    async with transaction(h.first):
        delivered = await add_gateway(h, status="completed", completed_at=OLD)
        terminal = await add_gateway(h, status="completed", completed_at=OLD)
        reclaimed = {
            delivered,
            terminal,
            await add_gateway(h, status="completed", completed_at=OLD),
            await add_gateway(h, status="completed", completed_at=OLD - timedelta(days=1)),
        }
        _ = await add_delivery(h, delivered, delivered=True, terminal=False)
        _ = await add_delivery(h, terminal, delivered=False, terminal=True)
        relayed = await add_gateway(h, status="completed", completed_at=OLD)
        relay = await add_delivery(h, relayed, delivered=False, terminal=False)
        kept = {
            relayed,
            await add_gateway(h, status="completed", completed_at=CUTOFF),
            await add_gateway(h, status="completed", completed_at=RECENT),
            await add_gateway(h, status="completed", completed_at=None),
            await add_gateway(h, status="pending", completed_at=OLD),
            await add_gateway(h, status="delivered", completed_at=OLD),
        }
    # When one batch runs, then another.
    table = TABLES["gateway_messages"]
    first = await purge_once(table, h.first, 100)
    second = await purge_once(table, h.first, 100)
    async with transaction(h.first):
        rows = await h.first.fetchall(
            "SELECT id, agent_id, content, result, status, guest_model_ref FROM gateway_messages"
        )
        deliveries = await h.first.fetchval("SELECT count(*) FROM gateway_report_deliveries")
        native = [await openclaw_reply_dao.is_native_run(h.agent_id, str(message_id)) for message_id in reclaimed]
        relay_source = await gateway_message_dao.get(relayed)
        claims = await gateway_report_delivery_dao.claim_batch(limit=10, lease_seconds=60, now=RECENT)
    # Then the first cleared exactly the expired payloads, the second found nothing, and no row or delivery went:
    # reclaimed runs are still native, and the pending relay is still claimed with its result intact.
    by_id = {row["id"]: row for row in rows}
    assert (first, second) == (len(reclaimed), 0)
    assert set(by_id) == reclaimed | kept
    assert {key for key, row in by_id.items() if (row["content"], row["result"]) == ("", None)} == reclaimed
    assert {key for key, row in by_id.items() if (row["content"], row["result"]) == ("question", "answer")} == kept
    assert {(row["agent_id"], row["guest_model_ref"]) for row in rows} == {(h.agent_id, "provider/model")}
    assert {str(by_id[key]["status"]) for key in reclaimed} == {"completed"}
    assert deliveries == 3
    assert native == [True] * len(reclaimed)
    assert relay_source is not None
    assert relay_source.result == "answer"
    assert [(claim.id, claim.message_id) for claim in claims] == [(relay, relayed)]


async def test_gateway_reclaim_batches_are_bounded_and_oldest_first(retention_db: RetentionDatabase) -> None:
    # Given five expired completed messages with distinct completion times.
    h = retention_db
    async with transaction(h.first):
        ids = [await seed_expired_gateway(h, minutes) for minutes in range(5)]
    table = TABLES["gateway_messages"]
    # When batches of two run until one comes back short.
    first = await purge_once(table, h.first, 2)
    left_after_first = await remaining(h, table)
    second = await purge_once(table, h.first, 2)
    third = await purge_once(table, h.first, 2)
    fourth = await purge_once(table, h.first, 2)
    # Then no statement exceeded the limit, and the first one took the two oldest.
    assert (first, second, third, fourth) == (2, 2, 1, 0)
    assert left_after_first == set(ids[2:])
    assert await remaining(h, table) == set()


async def test_idle_gateway_reclaim_reads_only_the_partial_index(
    retention_db: RetentionDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given thousands of rows an idle batch must not visit: already reclaimed completed messages and unfinished
    # traffic, beside a few completed messages still inside the window, all analyzed.
    h = retention_db
    async with transaction(h.first):
        await h.first.execute(
            "INSERT INTO gateway_messages (id, agent_id, content, status, result, created_at, completed_at) "
            "SELECT gen_random_uuid(), %(agent)s, CASE WHEN g %% 3 = 0 THEN '' ELSE 'question' END, "
            "CASE WHEN g %% 3 = 0 THEN 'completed' WHEN g %% 3 = 1 THEN 'pending' ELSE 'delivered' END, NULL, "
            "%(at)s, CASE WHEN g %% 3 = 0 THEN %(at)s + g * interval '1 minute' END FROM generate_series(1, 6000) g",
            {"agent": h.agent_id, "at": OLD},
        )
        recent = {await add_gateway(h, status="completed", completed_at=RECENT) for _ in range(3)}
        await h.first.execute("ANALYZE gateway_messages")
    captured: list[tuple[str, Params]] = []
    original = h.first.fetchall

    async def spy(query: str, params: Params = None) -> list[Row]:
        captured.append((query, params))
        return await original(query, params)

    monkeypatch.setattr(h.first, "fetchall", spy)
    reclaimed = await purge_once(TABLES["gateway_messages"], h.first, 5000)
    [(query, params)] = captured[-1:]
    # When the planner is asked about the exact statement the DAO sent, with every plan allowed.
    async with transaction(h.first):
        plan_rows = await original("EXPLAIN (COSTS OFF) " + query, params)
    plan = "\n".join(str(row["QUERY PLAN"]) for row in plan_rows)
    # Then nothing was due, and the batch reads the partial index rather than scanning gateway_messages.
    assert reclaimed == 0
    assert await remaining(h, TABLES["gateway_messages"]) == recent
    assert "ix_gateway_messages_completed_at" in plan
    assert "Seq Scan on gateway_messages" not in plan


class PassFinishedError(Exception):
    pass


@pytest.mark.parametrize("gateway_days", [0, MIN_GATEWAY_MESSAGE_RETENTION_DAYS])
async def test_worker_pass_reclaims_gateway_payloads_only_when_its_retention_is_set(
    retention_db: RetentionDatabase, monkeypatch: pytest.MonkeyPatch, gateway_days: int
) -> None:
    # Given an expired activity row and an expired completed message, and a clock whose cutoff for both tables is
    # CUTOFF when their retention is set.
    h = retention_db
    async with transaction(h.first):
        activity = await add_activity(h, OLD)
        message = await add_gateway(h, status="completed", completed_at=OLD)
    days = MIN_GATEWAY_MESSAGE_RETENTION_DAYS

    class Clock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return CUTOFF + timedelta(days=days)

    sleeps: list[float] = []

    async def finish_pass(seconds: float) -> None:
        sleeps.append(seconds)
        raise PassFinishedError

    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(worker, "sleep", finish_pass)
    monkeypatch.setattr(get_settings(), "ACTIVITY_LOG_RETENTION_DAYS", days)
    monkeypatch.setattr(get_settings(), "TRIGGER_EXECUTION_RETENTION_DAYS", 0)
    monkeypatch.setattr(get_settings(), "GATEWAY_MESSAGE_RETENTION_DAYS", gateway_days)
    # When one real worker pass runs on this schema.
    async with transaction(h.first):
        with pytest.raises(PassFinishedError):
            await worker.start_operational_log_retention_worker()
        activity_left = await h.first.fetchval(
            "SELECT count(*) FROM agent_activity_logs WHERE id = %(id)s", {"id": activity}
        )
        payload = await h.first.fetchone(
            "SELECT content, result FROM gateway_messages WHERE id = %(id)s", {"id": message}
        )
    # Then the pass ran without a failure into the poll sleep and purged the activity row, while the message kept
    # its payload at 0 days and lost it once its retention is set.
    assert sleeps == [worker.POLL_SECONDS]
    assert activity_left == 0
    expected = {"content": "question", "result": "answer"} if gateway_days == 0 else {"content": "", "result": None}
    assert payload == expected


@pytest.mark.parametrize("name", sorted(TABLES))
async def test_two_purgers_delete_disjoint_rows(retention_db: RetentionDatabase, name: str) -> None:
    # Given six expired rows.
    h = retention_db
    table = TABLES[name]
    async with transaction(h.first):
        _ = [await table.seed_expired(h, minutes) for minutes in range(6)]
    # When one purger deletes a batch and stays open while a second purger runs and commits.
    async with transaction(h.first):
        await h.first.execute(GUARD)
        first = await table.purge(h.first, 3)
        async with transaction(h.second):
            await h.second.execute(GUARD)
            with anyio.fail_after(FAIL_AFTER_SECONDS):
                second = await table.purge(h.second, 3)
    # Then the second one neither waited nor reused a row the first held, so together they took all six.
    assert (first, second) == (3, 3)
    assert await remaining(h, table) == set()


@pytest.mark.parametrize("name", sorted(TABLES))
async def test_purge_statement_can_use_the_cutoff_index(
    retention_db: RetentionDatabase, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # Given a seeded and analyzed table, and the exact statement the DAO sends (captured, not retyped).
    h = retention_db
    table = TABLES[name]
    async with transaction(h.first):
        if name == "trigger_executions":
            await h.first.execute(
                "INSERT INTO trigger_executions (id, trigger_id, agent_id, source, status, idempotency_key, payload, "
                "payload_text, scheduled_at, finished_at) SELECT gen_random_uuid(), %(trigger)s, %(agent)s, 'cron', "
                "CASE WHEN g %% 3 = 0 THEN 'pending' WHEN g %% 3 = 1 THEN 'completed' ELSE 'failed' END, "
                "'k' || g, '{}'::jsonb, '', %(at)s, CASE WHEN g %% 3 = 0 THEN NULL ELSE %(at)s + g * interval '1 minute' END "
                "FROM generate_series(1, 3000) g",
                {"trigger": h.trigger_id, "agent": h.agent_id, "at": OLD},
            )
        elif name == "gateway_messages":
            await h.first.execute(
                "INSERT INTO gateway_messages (id, agent_id, content, status, result, created_at, completed_at) "
                "SELECT gen_random_uuid(), %(agent)s, 'question', "
                "CASE WHEN g %% 3 = 0 THEN 'pending' WHEN g %% 3 = 1 THEN 'completed' ELSE 'delivered' END, "
                "CASE WHEN g %% 3 = 1 THEN 'answer' END, %(at)s, "
                "CASE WHEN g %% 3 = 1 THEN %(at)s + g * interval '1 minute' END FROM generate_series(1, 3000) g",
                {"agent": h.agent_id, "at": OLD},
            )
        elif name == "notifications":
            await h.first.execute(
                "INSERT INTO notifications (id, user_id, type, title, body, is_read, created_at) "
                "SELECT gen_random_uuid(), %(user)s, 'system', 't', 'b', g %% 2 = 0, %(at)s + g * interval '1 minute' "
                "FROM generate_series(1, 3000) g",
                {"user": h.user_id, "at": OLD},
            )
        elif name == "tasks":
            await h.first.execute(
                "INSERT INTO tasks (id, agent_id, title, type, status, priority, assignee, created_by, created_at, "
                "updated_at, completed_at) SELECT gen_random_uuid(), %(agent)s, 't', 'todo', "
                "(CASE WHEN g %% 3 = 0 THEN 'pending' ELSE 'done' END)::task_status_enum, 'low', 'agent', %(user)s, "
                "%(at)s, %(at)s + g * interval '1 minute', "
                "CASE WHEN g %% 3 = 1 THEN %(at)s + g * interval '1 minute' END FROM generate_series(1, 3000) g",
                {"agent": h.agent_id, "user": h.user_id, "at": OLD},
            )
            await h.first.execute(
                "INSERT INTO task_logs (id, task_id, content, created_at) "
                "SELECT gen_random_uuid(), id, 'log', %(at)s FROM tasks WHERE status = 'done'",
                {"at": OLD},
            )
        elif name == "channel_event_receipts":
            await h.first.execute(
                "INSERT INTO channel_event_receipts (config_id, provider, event_key, agent_id, fingerprint, created_at) "
                "SELECT %(config)s, 'feishu', gen_random_uuid()::text, %(agent)s, 'v1:' || repeat('a', 64), "
                "%(at)s + g * interval '1 minute' FROM generate_series(1, 3000) g",
                {"config": h.config_id, "agent": h.agent_id, "at": OLD},
            )
        elif name == "gateway_report_deliveries":
            await h.first.execute(
                "WITH m AS (INSERT INTO gateway_messages (id, agent_id, content, status, result, created_at, "
                "completed_at) SELECT gen_random_uuid(), %(agent)s, 'question', 'completed', 'answer', %(at)s, %(at)s "
                "FROM generate_series(1, 3000) RETURNING id) "
                "INSERT INTO gateway_report_deliveries (id, message_id, destination_kind, destination_id, available_at, "
                "delivered_at, terminal_reason, terminal_at) SELECT gen_random_uuid(), id, 'agent', %(agent)s, %(at)s, "
                "CASE WHEN n %% 3 = 1 THEN %(at)s + n * interval '1 minute' END, "
                "CASE WHEN n %% 3 = 2 THEN 'payload_rejected' END, "
                "CASE WHEN n %% 3 = 2 THEN %(at)s + n * interval '1 minute' END "
                "FROM (SELECT id, row_number() OVER () AS n FROM m) numbered",
                {"agent": h.agent_id, "at": OLD},
            )
        elif name == "workspace_file_revisions":
            await h.first.execute(
                "INSERT INTO workspace_file_revisions (id, agent_id, path, operation, actor_type, content_hash, "
                "created_at, updated_at) SELECT gen_random_uuid(), %(agent)s, 'file-' || (g %% 300), 'write', 'user', "
                "'hash', %(at)s + g * interval '1 minute', %(at)s + g * interval '1 minute' FROM generate_series(1, 3000) g",
                {"agent": h.agent_id, "at": OLD},
            )
        else:
            await h.first.execute(
                "INSERT INTO agent_activity_logs (id, agent_id, action_type, summary, created_at) "
                "SELECT gen_random_uuid(), %(agent)s, 'chat_reply', 's', %(at)s + g * interval '1 minute' "
                "FROM generate_series(1, 3000) g",
                {"agent": h.agent_id, "at": OLD},
            )
        await h.first.execute(f"ANALYZE {table.name}")
    captured: list[tuple[str, Params]] = []
    original = h.first.fetchall

    async def spy(query: str, params: Params = None) -> list[Row]:
        captured.append((query, params))
        return await original(query, params)

    monkeypatch.setattr(h.first, "fetchall", spy)
    # A batch far smaller than the table, as in production: with a limit above the row count a hash join is cheaper.
    _ = await purge_once(table, h.first, 50)
    [(query, params)] = captured[-1:]
    # When the planner is asked about that statement with sequential scans forbidden: on a table this small it
    # could pick one on cost alone, so a plan that names the index shows the predicate can use it at all.
    async with transaction(h.first):
        await h.first.execute("SET LOCAL enable_seqscan = off")
        plan_rows = await original("EXPLAIN (COSTS OFF) " + query, params)
    plan = "\n".join(str(row["QUERY PLAN"]) for row in plan_rows)
    # Then the cutoff scan runs on the index and deletes by TID rather than scanning the table.
    assert table.index in plan
    assert "Tid Scan" in plan
    assert "Seq Scan" not in plan
    if name == "workspace_file_revisions":
        # The oldest-first scan itself walks the created_at index, and the newer-revision probe the per-file one.
        assert "ix_workspace_file_revisions_created_at" in plan


async def test_baseline_creates_the_append_only_cutoff_indexes_idempotently(retention_db: RetentionDatabase) -> None:
    # Given the baseline applied twice by the fixture.
    h = retention_db
    names = [table.index for name, table in TABLES.items() if name in NEW_TABLES] + [
        "ix_web_search_export_payloads_created_at"
    ]
    async with transaction(h.first):
        rows = await h.first.fetchall(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = ANY(%(names)s)",
            {"names": names},
        )
    # Then each index exists once, the task and delivery ones partial on the rows a purge may take.
    definitions = {str(row["indexname"]): str(row["indexdef"]) for row in rows}
    assert set(definitions) == set(names)
    assert "COALESCE(completed_at, updated_at)" in definitions["ix_tasks_done_finished_at"]
    assert "status = 'done'" in definitions["ix_tasks_done_finished_at"]
    assert "COALESCE(delivered_at, terminal_at)" in definitions["ix_gateway_report_deliveries_finished_at"]
    assert "delivered_at IS NOT NULL" in definitions["ix_gateway_report_deliveries_finished_at"]
    assert "(agent_id, path, created_at DESC, id DESC)" in definitions["ix_workspace_file_revisions_file_created"]


@pytest.mark.parametrize("name", NEW_TABLES)
async def test_new_table_purge_batches_are_bounded_and_oldest_first(retention_db: RetentionDatabase, name: str) -> None:
    # Given five expired rows with distinct ages.
    h = retention_db
    table = TABLES[name]
    async with transaction(h.first):
        ids = [await table.seed_expired(h, minutes) for minutes in range(5)]
    # When batches of two are purged until one comes back short.
    first = await purge_once(table, h.first, 2)
    left_after_first = await remaining(h, table)
    second = await purge_once(table, h.first, 2)
    third = await purge_once(table, h.first, 2)
    fourth = await purge_once(table, h.first, 2)
    # Then no statement exceeded the limit, and the first one took the two oldest.
    assert (first, second, third, fourth) == (2, 2, 1, 0)
    assert left_after_first == set(ids[2:])
    assert await remaining(h, table) == set()


async def test_notification_purge_deletes_read_unread_and_archived_rows_older_than_the_cutoff_only(
    retention_db: RetentionDatabase,
) -> None:
    # Given old notifications in every state, and notifications at and after the cutoff.
    h = retention_db
    async with transaction(h.first):
        purged = {
            await add_notification(h, OLD, is_read=True),
            await add_notification(h, OLD - timedelta(days=30)),
            await add_notification(h, CUTOFF - timedelta(microseconds=1)),
        }
        archived = await add_notification(h, OLD)
        await h.first.execute(
            "UPDATE notifications SET archived_at = %(at)s WHERE id = %(id)s", {"at": OLD, "id": archived}
        )
        purged.add(archived)
        kept = {await add_notification(h, CUTOFF), await add_notification(h, RECENT, is_read=True)}
    # When one batch runs.
    deleted = await purge_once(TABLES["notifications"], h.first, 100)
    # Then every old notification went and the others stay.
    assert deleted == len(purged)
    assert await remaining(h, TABLES["notifications"]) == kept


async def test_task_purge_deletes_only_done_tasks_with_their_logs(retention_db: RetentionDatabase) -> None:
    # Given done tasks past the cutoff (one never stamped completed_at), and tasks that must stay: done ones inside
    # the window, a done one completed recently but touched long ago, and unfinished ones however old.
    h = retention_db
    async with transaction(h.first):
        purged = {
            await add_task(h, status="done", completed_at=OLD),
            await add_task(h, status="done", completed_at=OLD - timedelta(days=9), logs=0),
            await add_task(h, status="done", completed_at=None, updated_at=OLD),
        }
        kept = {
            await add_task(h, status="done", completed_at=CUTOFF),
            await add_task(h, status="done", completed_at=RECENT),
            await add_task(h, status="done", completed_at=None, updated_at=RECENT),
            await add_task(h, status="done", completed_at=RECENT, updated_at=OLD),
            await add_task(h, status="pending", completed_at=None, updated_at=OLD),
            await add_task(h, status="doing", completed_at=None, updated_at=OLD),
            await add_task(h, status="pending", completed_at=OLD),
        }
    # When one batch runs, then another.
    first = await purge_once(TABLES["tasks"], h.first, 100)
    second = await purge_once(TABLES["tasks"], h.first, 100)
    async with transaction(h.first):
        logged = {row["task_id"] for row in await h.first.fetchall("SELECT DISTINCT task_id FROM task_logs")}
        log_count = await h.first.fetchval("SELECT count(*) FROM task_logs")
    # Then exactly the finished tasks went, and each kept task still has both of its logs and no log is orphaned.
    assert (first, second) == (len(purged), 0)
    assert await remaining(h, TABLES["tasks"]) == kept
    assert logged == kept
    assert log_count == 2 * len(kept)


async def test_receipt_purge_deletes_old_receipts_with_their_cascaded_rows_and_keeps_the_rest(
    retention_db: RetentionDatabase,
) -> None:
    # Given old receipts (two with a media descriptor and a burst outcome), and receipts after the cutoff with them too.
    h = retention_db
    async with transaction(h.first):
        purged = {
            await add_receipt(h, OLD, dependents=True),
            await add_receipt(h, OLD - timedelta(days=30), dependents=True),
            await add_receipt(h, OLD),
        }
        kept = {await add_receipt(h, CUTOFF, dependents=True), await add_receipt(h, RECENT, dependents=True)}
    # When one batch runs.
    deleted = await purge_once(TABLES["channel_event_receipts"], h.first, 100)
    async with transaction(h.first):
        media = await h.first.fetchall("SELECT event_key FROM channel_event_media")
        outcomes = await h.first.fetchall("SELECT event_key FROM personal_burst_inputs")
    # Then the old receipts went with their media descriptors and burst outcomes, and the others kept every row.
    assert deleted == len(purged)
    assert await remaining(h, TABLES["channel_event_receipts"]) == kept
    assert {UUID(str(row["event_key"])) for row in media} == kept
    assert {UUID(str(row["event_key"])) for row in outcomes} == kept


async def test_delivery_purge_deletes_only_finished_deliveries_and_keeps_pending_ones_and_every_message(
    retention_db: RetentionDatabase,
) -> None:
    # Given old delivered and terminal deliveries, and ones that must stay: finished inside the window, and pending
    # ones however old (waiting, and leased by a relay).
    h = retention_db
    async with transaction(h.first):
        delivered = await add_gateway(h, status="completed", completed_at=OLD)
        terminal = await add_gateway(h, status="completed", completed_at=OLD)
        recent_delivered = await add_gateway(h, status="completed", completed_at=OLD)
        recent_terminal = await add_gateway(h, status="completed", completed_at=OLD)
        waiting = await add_gateway(h, status="completed", completed_at=OLD)
        leased = await add_gateway(h, status="completed", completed_at=OLD)
        purged = {
            await add_delivery(h, delivered, delivered=True, terminal=False, at=OLD),
            await add_delivery(h, terminal, delivered=False, terminal=True, at=OLD - timedelta(days=9)),
        }
        kept = {
            await add_delivery(h, recent_delivered, delivered=True, terminal=False, at=RECENT),
            await add_delivery(h, recent_terminal, delivered=False, terminal=True, at=CUTOFF),
            await add_delivery(h, waiting, delivered=False, terminal=False),
            leased_delivery := await add_delivery(h, leased, delivered=False, terminal=False),
        }
        await h.first.execute(
            "UPDATE gateway_report_deliveries SET claim_token = %(token)s, lease_expires_at = %(at)s WHERE id = %(id)s",
            {"token": uuid4(), "at": OLD, "id": leased_delivery},
        )
    # When one batch runs, then another.
    first = await purge_once(TABLES["gateway_report_deliveries"], h.first, 100)
    second = await purge_once(TABLES["gateway_report_deliveries"], h.first, 100)
    async with transaction(h.first):
        messages = await h.first.fetchval("SELECT count(*) FROM gateway_messages")
    # Then exactly the finished deliveries went, the pending ones stay claimable, and no message row was touched.
    assert (first, second) == (len(purged), 0)
    assert await remaining(h, TABLES["gateway_report_deliveries"]) == kept
    assert messages == 6


async def test_revision_purge_never_deletes_the_newest_revision_of_a_file(retention_db: RetentionDatabase) -> None:
    # Given three old revisions of one file, an old file with a single revision, the same path under another agent,
    # two old revisions that share a timestamp, and a superseded revision still inside the window.
    h = retention_db
    other_agent = await add_agent(h)
    async with transaction(h.first):
        oldest = await add_revision(h, "a.md", OLD - timedelta(days=2))
        middle = await add_revision(h, "a.md", OLD - timedelta(days=1))
        newest = await add_revision(h, "a.md", OLD)
        only = await add_revision(h, "single.md", OLD - timedelta(days=5))
        other_agents = await add_revision(h, "a.md", OLD - timedelta(days=5), agent_id=other_agent)
        twin_one = await add_revision(h, "twin.md", OLD)
        twin_two = await add_revision(h, "twin.md", OLD)
        inside_old = await add_revision(h, "window.md", CUTOFF)
        inside_new = await add_revision(h, "window.md", RECENT)
    # When one batch runs, then another.
    first = await purge_once(TABLES["workspace_file_revisions"], h.first, 100)
    second = await purge_once(TABLES["workspace_file_revisions"], h.first, 100)
    async with transaction(h.first):
        rows = await h.first.fetchall("SELECT id FROM workspace_file_revisions")
    left = {row["id"] for row in rows}
    # Then only superseded revisions past the cutoff went: the newest of every file stays, and of two revisions with
    # one timestamp exactly one stays.
    assert (first, second) == (3, 0)
    assert {oldest, middle}.isdisjoint(left)
    assert {newest, only, other_agents, inside_old, inside_new} <= left
    assert len({twin_one, twin_two} & left) == 1
    assert len(left) == 6


async def test_revision_purge_leaves_the_newest_revision_when_batches_run_in_parallel(
    retention_db: RetentionDatabase,
) -> None:
    # Given one file with six old revisions.
    h = retention_db
    async with transaction(h.first):
        ids = [await add_revision(h, "busy.md", OLD + timedelta(minutes=minutes)) for minutes in range(6)]
    # When one purger deletes a batch and stays open while a second purger runs and commits.
    async with transaction(h.first):
        await h.first.execute(GUARD)
        first = await purge_revisions(h.first, 3)
        async with transaction(h.second):
            await h.second.execute(GUARD)
            with anyio.fail_after(FAIL_AFTER_SECONDS):
                second = await purge_revisions(h.second, 3)
    # Then the second neither waited nor took a row the first held, and the newest revision survived both.
    assert (first, second) == (3, 2)
    async with transaction(h.first):
        rows = await h.first.fetchall("SELECT id FROM workspace_file_revisions")
    assert {row["id"] for row in rows} == {ids[-1]}


@pytest.mark.parametrize("enabled", [False, True])
async def test_worker_pass_purges_every_append_only_table_only_when_its_retention_is_set(
    retention_db: RetentionDatabase, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    # Given an expired row and a protected row in each opt-in table, and a clock whose cutoff for all of them is
    # CUTOFF when their retention is set.
    h = retention_db
    days = MIN_CHANNEL_EVENT_RECEIPT_RETENTION_DAYS + 3
    async with transaction(h.first):
        expired = {
            "notifications": await add_notification(h, OLD),
            "tasks": await add_task(h, status="done", completed_at=OLD),
            "channel_event_receipts": await add_receipt(h, OLD, dependents=True),
            "gateway_report_deliveries": await add_delivery(
                h, await add_gateway(h, status="completed", completed_at=OLD), delivered=True, terminal=False
            ),
            "workspace_file_revisions": await add_revision(h, "worker.md", OLD),
        }
        protected = {
            "notifications": await add_notification(h, RECENT),
            "tasks": await add_task(h, status="pending", completed_at=None, updated_at=OLD),
            "channel_event_receipts": await add_receipt(h, RECENT, dependents=True),
            "gateway_report_deliveries": await add_delivery(
                h, await add_gateway(h, status="completed", completed_at=OLD), delivered=False, terminal=False
            ),
            "workspace_file_revisions": await add_revision(h, "worker.md", OLD + timedelta(days=1)),
        }

    class Clock:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return CUTOFF + timedelta(days=days)

    async def finish_pass(seconds: float) -> None:
        raise PassFinishedError

    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(worker, "sleep", finish_pass)
    for setting in (
        "NOTIFICATION_RETENTION_DAYS",
        "TASK_RETENTION_DAYS",
        "CHANNEL_EVENT_RECEIPT_RETENTION_DAYS",
        "GATEWAY_REPORT_DELIVERY_RETENTION_DAYS",
        "WORKSPACE_REVISION_RETENTION_DAYS",
    ):
        monkeypatch.setattr(get_settings(), setting, days if enabled else 0)
    for setting in (
        "ACTIVITY_LOG_RETENTION_DAYS",
        "TRIGGER_EXECUTION_RETENTION_DAYS",
        "GATEWAY_MESSAGE_RETENTION_DAYS",
    ):
        monkeypatch.setattr(get_settings(), setting, 0)
    # When one real worker pass runs on this schema.
    async with transaction(h.first):
        with pytest.raises(PassFinishedError):
            await worker.start_operational_log_retention_worker()
    # Then with retention set the expired rows are gone and the protected ones stay; with it at 0 nothing is deleted.
    for name in NEW_TABLES:
        present = await remaining(h, TABLES[name])
        assert protected[name] in present
        assert (expired[name] in present) is not enabled
    async with transaction(h.first):
        dependents = await h.first.fetchval("SELECT count(*) FROM channel_event_media")
    assert dependents == (1 if enabled else 2)
