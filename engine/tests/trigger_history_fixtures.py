from collections.abc import Iterator
from datetime import UTC, datetime
from typing import override
from uuid import UUID

import pytest
from tests.test_crud_perf import _FakeCursor, _FakePool, _FakeRawConnection

from app.core.json_types import object_attr, object_mapping_from
from app.db import session as session_module
from app.records.trigger import TriggerExecutionRecord

HISTORY_COLUMNS = (
    "id",
    "trigger_id",
    "agent_id",
    "source",
    "status",
    "scheduled_at",
    "started_at",
    "finished_at",
    "created_at",
)
CREATED_AT = datetime(2026, 10, 23, 1, 2, 3, tzinfo=UTC)
AGENT_ID = UUID(int=1000)
TRIGGER_ID = UUID(int=2000)
SECRET = "history-private-token-secret-payload-error"


def execution(number: int, *, created_at: datetime = CREATED_AT) -> TriggerExecutionRecord:
    return TriggerExecutionRecord(
        id=UUID(int=number),
        agent_id=AGENT_ID,
        trigger_id=TRIGGER_ID,
        source="webhook",
        status="pending",
        scheduled_at=created_at,
        created_at=created_at,
        idempotency_key=SECRET,
        payload={"token": SECRET},
        payload_text=SECRET,
        lease_owner=SECRET,
        last_error=SECRET,
    )


def position(record: TriggerExecutionRecord) -> tuple[datetime, UUID]:
    assert record.created_at is not None
    return record.created_at, record.id


class HistoryCursor(_FakeCursor):
    def __init__(self, raw: HistoryRaw) -> None:
        super().__init__(raw)
        self.raw: HistoryRaw = raw
        self.results: list[dict[str, object]] = []

    @override
    async def execute(self, query: str, params: object = None) -> None:
        values = object_mapping_from(params)
        sql = " ".join(query.split())
        expected = (
            "SELECT id, trigger_id, agent_id, source, status, scheduled_at, started_at, finished_at, created_at "
            "FROM trigger_executions WHERE agent_id = %(agent_id)s AND trigger_id = %(trigger_id)s"
        )
        before = None
        if "before_created_at" in values:
            expected += " AND (created_at, id) < (%(before_created_at)s, %(before_id)s)"
            date, row_id = values["before_created_at"], values["before_id"]
            assert isinstance(date, datetime)
            assert isinstance(row_id, UUID)
            before = date, row_id
        expected += " ORDER BY created_at DESC, id DESC LIMIT %(limit)s"
        assert sql == expected, "History must use only the scoped, projected, lock-free SELECT"
        limit = values["limit"]
        assert isinstance(limit, int)
        assert 1 <= limit <= 101
        rows = sorted(
            (
                row
                for row in self.raw.rows.values()
                if row.agent_id == values["agent_id"]
                and row.trigger_id == values["trigger_id"]
                and (before is None or position(row) < before)
            ),
            key=position,
            reverse=True,
        )
        self.raw.queries.append((sql, values))
        self.results = [{column: object_attr(row, column) for column in HISTORY_COLUMNS} for row in rows[:limit]]

    @override
    async def fetchall(self) -> list[dict[str, object]]:
        return self.results


class HistoryRaw(_FakeRawConnection):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[UUID, TriggerExecutionRecord] = {}
        self.queries: list[tuple[str, dict[str, object]]] = []

    @override
    def cursor(self) -> HistoryCursor:
        return HistoryCursor(self)


@pytest.fixture
def history_raw(monkeypatch: pytest.MonkeyPatch) -> Iterator[HistoryRaw]:
    raw = HistoryRaw()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    token = session_module._conn_ctx.set(None)
    try:
        yield raw
    finally:
        session_module._conn_ctx.reset(token)
