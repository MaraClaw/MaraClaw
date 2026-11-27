"""SQL-shape tests for the retention batches of every opt-in table (no live Postgres)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, override

import pytest

from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.dao.notification_dao import notification_dao
from app.dao.task_dao import task_dao
from app.dao.trigger_dao import trigger_execution_dao
from app.dao.workspace_dao import workspace_file_revision_dao
from app.db import session as session_module
from app.scripts import bootstrap_db
from test_crud_perf import _FakeCursor, _FakePool, _FakeRawConnection

CUTOFF = datetime(2026, 11, 12, tzinfo=UTC)


class PurgeCursor(_FakeCursor):
    def __init__(self, parent: PurgeConnection) -> None:
        super().__init__(parent)
        self.parent = parent

    @override
    async def fetchall(self) -> list[Any]:
        return [{"id": index} for index in range(self.parent.deleted)]


class PurgeConnection(_FakeRawConnection):
    """Answers every statement as a DELETE ... RETURNING id that removed ``deleted`` rows."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted = 0

    @override
    def cursor(self) -> PurgeCursor:
        return PurgeCursor(self)


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> Iterator[PurgeConnection]:
    conn = PurgeConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(conn))
    token = session_module._conn_ctx.set(None)
    yield conn
    session_module._conn_ctx.reset(token)


def squashed(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()


async def test_activity_purge_deletes_one_bounded_oldest_first_batch_in_its_own_transaction(
    raw: PurgeConnection,
) -> None:
    # Given a statement that removes three rows.
    raw.deleted = 3
    # When the activity logs are purged with a limit of five.
    deleted = await agent_activity_log_dao.purge_older_than(older_than=CUTOFF, limit=5)
    # Then one statement ran, committed on its own, bounded by the limit and skipping rows held elsewhere.
    assert deleted == 3
    assert raw.commits == 1
    [(query, params)] = raw.executed
    assert squashed(query) == (
        "WITH expired AS ( SELECT ctid FROM agent_activity_logs WHERE created_at < %(cutoff)s "
        "ORDER BY created_at LIMIT %(limit)s FOR UPDATE SKIP LOCKED ) "
        "DELETE FROM agent_activity_logs WHERE ctid = ANY(ARRAY(SELECT ctid FROM expired)) RETURNING id"
    )
    assert params == {"cutoff": CUTOFF, "limit": 5}


async def test_execution_purge_only_selects_finished_terminal_rows_in_one_bounded_batch(raw: PurgeConnection) -> None:
    # Given a statement that removes two rows.
    raw.deleted = 2
    # When finished executions are purged with a limit of five.
    deleted = await trigger_execution_dao.purge_finished_before(finished_before=CUTOFF, limit=5)
    # Then one statement ran, committed on its own, and selects by terminal status and finished_at only.
    assert deleted == 2
    assert raw.commits == 1
    [(query, params)] = raw.executed
    assert squashed(query) == (
        "WITH expired AS ( SELECT ctid FROM trigger_executions "
        "WHERE status IN ('completed', 'failed') AND finished_at IS NOT NULL AND finished_at < %(cutoff)s "
        "ORDER BY finished_at LIMIT %(limit)s FOR UPDATE SKIP LOCKED ) "
        "DELETE FROM trigger_executions WHERE ctid = ANY(ARRAY(SELECT ctid FROM expired)) RETURNING id"
    )
    assert params == {"cutoff": CUTOFF, "limit": 5}


async def test_gateway_reclaim_clears_payloads_of_one_bounded_oldest_first_batch_in_its_own_transaction(
    raw: PurgeConnection,
) -> None:
    # Given a statement that reclaims four messages.
    raw.deleted = 4
    # When completed gateway messages are reclaimed with a limit of five.
    reclaimed = await gateway_message_dao.reclaim_completed_before(completed_before=CUTOFF, limit=5)
    # Then one statement ran, committed on its own, and cleared only the payload of completed rows with no pending
    # relay, bounded by the limit and skipping rows held elsewhere.
    assert reclaimed == 4
    assert raw.commits == 1
    [(query, params)] = raw.executed
    assert squashed(query) == (
        "WITH due AS ( SELECT m.ctid FROM gateway_messages AS m "
        "WHERE m.status = 'completed' AND m.result IS NOT NULL AND m.completed_at < %(cutoff)s "
        "AND NOT EXISTS ( SELECT 1 FROM gateway_report_deliveries AS d "
        "WHERE d.message_id = m.id AND d.delivered_at IS NULL AND d.terminal_at IS NULL ) "
        "ORDER BY m.completed_at LIMIT %(limit)s FOR UPDATE OF m SKIP LOCKED ) "
        "UPDATE gateway_messages SET content = '', result = NULL "
        "WHERE ctid = ANY(ARRAY(SELECT ctid FROM due)) RETURNING id"
    )
    assert params == {"cutoff": CUTOFF, "limit": 5}


type Purge = Callable[[], Awaitable[int]]

PURGES: dict[str, Purge] = {
    "activity": lambda: agent_activity_log_dao.purge_older_than(older_than=CUTOFF, limit=5),
    "execution": lambda: trigger_execution_dao.purge_finished_before(finished_before=CUTOFF, limit=5),
    "gateway": lambda: gateway_message_dao.reclaim_completed_before(completed_before=CUTOFF, limit=5),
    "notification": lambda: notification_dao.purge_older_than(older_than=CUTOFF, limit=5),
    "task": lambda: task_dao.purge_finished_before(finished_before=CUTOFF, limit=5),
    "receipt": lambda: channel_event_receipt_dao.purge_older_than(older_than=CUTOFF, limit=5),
    "delivery": lambda: gateway_report_delivery_dao.purge_finished_before(finished_before=CUTOFF, limit=5),
    "revision": lambda: workspace_file_revision_dao.purge_superseded_before(created_before=CUTOFF, limit=5),
}


async def test_notification_purge_deletes_one_bounded_oldest_first_batch_in_its_own_transaction(
    raw: PurgeConnection,
) -> None:
    # Given a statement that removes three rows.
    raw.deleted = 3
    # When notifications are purged with a limit of five.
    deleted = await notification_dao.purge_older_than(older_than=CUTOFF, limit=5)
    # Then one statement ran, committed on its own, bounded by the limit and skipping rows held elsewhere.
    assert deleted == 3
    assert raw.commits == 1
    [(query, params)] = raw.executed
    assert squashed(query) == (
        "WITH expired AS ( SELECT ctid FROM notifications WHERE created_at < %(cutoff)s "
        "ORDER BY created_at LIMIT %(limit)s FOR UPDATE SKIP LOCKED ) "
        "DELETE FROM notifications WHERE ctid = ANY(ARRAY(SELECT ctid FROM expired)) RETURNING id"
    )
    assert params == {"cutoff": CUTOFF, "limit": 5}


async def test_task_purge_only_selects_done_tasks_and_deletes_their_logs_in_the_same_statement(
    raw: PurgeConnection,
) -> None:
    # Given a statement that removes two tasks.
    raw.deleted = 2
    # When finished tasks are purged with a limit of five.
    deleted = await task_dao.purge_finished_before(finished_before=CUTOFF, limit=5)
    # Then one statement ran and committed on its own; it picks done tasks by their finish time, bounded and skipping
    # rows held elsewhere, and removes the picked tasks' logs before the tasks.
    assert deleted == 2
    assert raw.commits == 1
    [(query, params)] = raw.executed
    assert squashed(query) == (
        "WITH expired AS ( SELECT ctid, id FROM tasks "
        "WHERE status = 'done' AND COALESCE(completed_at, updated_at) < %(cutoff)s "
        "ORDER BY COALESCE(completed_at, updated_at) LIMIT %(limit)s FOR UPDATE SKIP LOCKED ), "
        "logs AS ( DELETE FROM task_logs WHERE task_id IN (SELECT id FROM expired) ) "
        "DELETE FROM tasks WHERE ctid = ANY(ARRAY(SELECT ctid FROM expired)) RETURNING id"
    )
    assert params == {"cutoff": CUTOFF, "limit": 5}


async def test_receipt_purge_deletes_one_bounded_oldest_first_batch_in_its_own_transaction(
    raw: PurgeConnection,
) -> None:
    # Given a statement that removes three receipts.
    raw.deleted = 3
    # When receipts are purged with a limit of five.
    deleted = await channel_event_receipt_dao.purge_older_than(older_than=CUTOFF, limit=5)
    # Then one statement ran, committed on its own, bounded by the limit and skipping rows held elsewhere.
    assert deleted == 3
    assert raw.commits == 1
    [(query, params)] = raw.executed
    assert squashed(query) == (
        "WITH expired AS ( SELECT ctid FROM channel_event_receipts WHERE created_at < %(cutoff)s "
        "ORDER BY created_at LIMIT %(limit)s FOR UPDATE SKIP LOCKED ) "
        "DELETE FROM channel_event_receipts WHERE ctid = ANY(ARRAY(SELECT ctid FROM expired)) RETURNING event_key"
    )
    assert params == {"cutoff": CUTOFF, "limit": 5}


async def test_delivery_purge_only_selects_delivered_or_terminal_rows_in_one_bounded_batch(
    raw: PurgeConnection,
) -> None:
    # Given a statement that removes four deliveries.
    raw.deleted = 4
    # When finished deliveries are purged with a limit of five.
    deleted = await gateway_report_delivery_dao.purge_finished_before(finished_before=CUTOFF, limit=5)
    # Then one statement ran, committed on its own, and selects only rows with a delivered or terminal stamp.
    assert deleted == 4
    assert raw.commits == 1
    [(query, params)] = raw.executed
    assert squashed(query) == (
        "WITH expired AS ( SELECT ctid FROM gateway_report_deliveries "
        "WHERE (delivered_at IS NOT NULL OR terminal_at IS NOT NULL) "
        "AND COALESCE(delivered_at, terminal_at) < %(cutoff)s "
        "ORDER BY COALESCE(delivered_at, terminal_at) LIMIT %(limit)s FOR UPDATE SKIP LOCKED ) "
        "DELETE FROM gateway_report_deliveries WHERE ctid = ANY(ARRAY(SELECT ctid FROM expired)) RETURNING id"
    )
    assert params == {"cutoff": CUTOFF, "limit": 5}


async def test_revision_purge_only_selects_revisions_with_a_newer_one_in_one_bounded_batch(
    raw: PurgeConnection,
) -> None:
    # Given a statement that removes two revisions.
    raw.deleted = 2
    # When superseded revisions are purged with a limit of five.
    deleted = await workspace_file_revision_dao.purge_superseded_before(created_before=CUTOFF, limit=5)
    # Then one statement ran, committed on its own, and picks only a revision another one of its file follows.
    assert deleted == 2
    assert raw.commits == 1
    [(query, params)] = raw.executed
    assert squashed(query) == (
        "WITH superseded AS ( SELECT r.ctid FROM workspace_file_revisions AS r WHERE r.created_at < %(cutoff)s "
        "AND EXISTS ( SELECT 1 FROM workspace_file_revisions AS n WHERE n.agent_id = r.agent_id "
        "AND n.path = r.path AND (n.created_at, n.id) > (r.created_at, r.id) ) "
        "ORDER BY r.created_at LIMIT %(limit)s FOR UPDATE OF r SKIP LOCKED ) "
        "DELETE FROM workspace_file_revisions WHERE ctid = ANY(ARRAY(SELECT ctid FROM superseded)) RETURNING id"
    )
    assert params == {"cutoff": CUTOFF, "limit": 5}


@pytest.mark.parametrize("purge", sorted(PURGES))
async def test_purge_reports_zero_when_nothing_matched(raw: PurgeConnection, purge: str) -> None:
    # Given a statement that removes nothing.
    raw.deleted = 0
    # When any purge runs.
    deleted = await PURGES[purge]()
    # Then it returns zero after a single statement.
    assert deleted == 0
    assert len(raw.executed) == 1


async def test_execution_purge_predicate_is_the_partial_index_predicate(raw: PurgeConnection) -> None:
    # Given the baseline index the purge relies on and the statement the purge sends.
    baseline = squashed(bootstrap_db.SCHEMA_BASELINE.read_text())
    _ = await trigger_execution_dao.purge_finished_before(finished_before=CUTOFF, limit=5)
    [(query, _params)] = raw.executed
    # Then the index is declared idempotently on finished_at, and the purge repeats its predicate verbatim.
    predicate = "status IN ('completed', 'failed')"
    assert (
        f"CREATE INDEX IF NOT EXISTS ix_trigger_executions_finished_at ON trigger_executions (finished_at) "
        f"WHERE {predicate}$ddl$);"
    ) in baseline
    assert predicate in squashed(query)


async def test_gateway_reclaim_predicate_is_the_partial_index_predicate(raw: PurgeConnection) -> None:
    # Given the baseline index the reclaim relies on and the statement the reclaim sends.
    baseline = squashed(bootstrap_db.SCHEMA_BASELINE.read_text())
    _ = await gateway_message_dao.reclaim_completed_before(completed_before=CUTOFF, limit=5)
    [(query, _params)] = raw.executed
    # Then the index is declared idempotently on completed_at, and the reclaim repeats its predicate on that table.
    assert (
        "CREATE INDEX IF NOT EXISTS ix_gateway_messages_completed_at ON gateway_messages (completed_at) "
        "WHERE status = 'completed' AND result IS NOT NULL$ddl$);"
    ) in baseline
    assert "m.status = 'completed' AND m.result IS NOT NULL" in squashed(query)


@pytest.mark.parametrize(
    ("index", "predicate", "purge"),
    [
        (
            "ix_tasks_done_finished_at ON tasks ((COALESCE(completed_at, updated_at))) WHERE status = 'done'",
            "status = 'done' AND COALESCE(completed_at, updated_at) < %(cutoff)s",
            "task",
        ),
        (
            (
                "ix_gateway_report_deliveries_finished_at ON gateway_report_deliveries "
                "((COALESCE(delivered_at, terminal_at))) WHERE delivered_at IS NOT NULL OR terminal_at IS NOT NULL"
            ),
            "(delivered_at IS NOT NULL OR terminal_at IS NOT NULL) AND COALESCE(delivered_at, terminal_at) < %(cutoff)s",
            "delivery",
        ),
    ],
)
async def test_partial_index_purge_predicates_repeat_the_index_predicate(
    raw: PurgeConnection, index: str, predicate: str, purge: str
) -> None:
    # Given the baseline index a purge relies on and the statement the purge sends.
    baseline = squashed(bootstrap_db.SCHEMA_BASELINE.read_text())
    _ = await PURGES[purge]()
    [(query, _params)] = raw.executed
    # Then the index is declared idempotently, and the purge repeats its predicate and expression verbatim.
    assert f"CREATE INDEX IF NOT EXISTS {index}$ddl$);" in baseline
    assert predicate in squashed(query)
