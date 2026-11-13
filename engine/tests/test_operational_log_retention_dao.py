"""SQL-shape tests for the activity log and trigger execution purge methods (no live Postgres)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, override

import pytest

from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.trigger_dao import trigger_execution_dao
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


@pytest.mark.parametrize("purge", ["activity", "execution"])
async def test_purge_reports_zero_when_nothing_matched(raw: PurgeConnection, purge: str) -> None:
    # Given a statement that removes nothing.
    raw.deleted = 0
    # When either purge runs.
    if purge == "activity":
        deleted = await agent_activity_log_dao.purge_older_than(older_than=CUTOFF, limit=5)
    else:
        deleted = await trigger_execution_dao.purge_finished_before(finished_before=CUTOFF, limit=5)
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
        f"WHERE {predicate};"
    ) in baseline
    assert predicate in squashed(query)
