from dataclasses import asdict, replace
from datetime import timedelta
from uuid import UUID

import pytest
from tests.trigger_history_fixtures import (
    AGENT_ID,
    CREATED_AT,
    HISTORY_COLUMNS,
    TRIGGER_ID,
    HistoryRaw,
    execution,
    history_raw as history_raw,
)

from app.dao.trigger_dao import trigger_execution_dao


async def test_history_orders_equal_timestamps_by_id_and_uses_strict_boundary(history_raw: HistoryRaw) -> None:
    # Given
    history_raw.rows = {UUID(int=i): execution(i) for i in range(1, 6)}
    history_raw.rows[UUID(int=6)] = execution(6, created_at=CREATED_AT - timedelta(seconds=1))
    # When
    first = await trigger_execution_dao.list_history_for_trigger(AGENT_ID, TRIGGER_ID, limit=3)
    second = await trigger_execution_dao.list_history_for_trigger(
        AGENT_ID, TRIGGER_ID, limit=3, before=(first[-1].created_at, first[-1].id)
    )
    # Then
    assert [row.id.int for row in first] == [5, 4, 3]
    assert [row.id.int for row in second] == [2, 1, 6]


async def test_history_continues_after_new_insert_and_reports_updated_stored_status(history_raw: HistoryRaw) -> None:
    # Given
    history_raw.rows = {UUID(int=i): execution(i) for i in range(1, 5)}
    first = await trigger_execution_dao.list_history_for_trigger(AGENT_ID, TRIGGER_ID, limit=2)
    history_raw.rows[UUID(int=5)] = execution(5, created_at=CREATED_AT + timedelta(seconds=1))
    history_raw.rows[UUID(int=2)].status = "future-provider-status"
    history_raw.rows[UUID(int=2)].source = "future-provider-source"
    # When
    second = await trigger_execution_dao.list_history_for_trigger(
        AGENT_ID, TRIGGER_ID, limit=2, before=(first[-1].created_at, first[-1].id)
    )
    # Then
    assert [row.id.int for row in second] == [2, 1]
    assert (second[0].status, second[0].source) == ("future-provider-status", "future-provider-source")


async def test_history_scopes_both_ids_projects_only_safe_fields_and_is_read_only(history_raw: HistoryRaw) -> None:
    # Given
    history_raw.rows = {
        UUID(int=1): execution(1),
        UUID(int=2): replace(execution(2), agent_id=UUID(int=999)),
        UUID(int=3): replace(execution(3), trigger_id=UUID(int=999)),
    }
    before = {key: asdict(row) for key, row in history_raw.rows.items()}
    # When
    rows = await trigger_execution_dao.list_history_for_trigger(AGENT_ID, TRIGGER_ID)
    # Then
    assert [row.id.int for row in rows] == [1]
    assert set(asdict(rows[0])) == set(HISTORY_COLUMNS)
    assert {key: asdict(row) for key, row in history_raw.rows.items()} == before
    assert len(history_raw.queries) == 1


@pytest.mark.parametrize(("requested", "expected"), [(0, 1), (1, 1), (101, 101), (10000, 101)])
async def test_history_bounds_dao_lookahead(history_raw: HistoryRaw, requested: int, expected: int) -> None:
    # Given
    history_raw.rows = {UUID(int=i): execution(i) for i in range(1, 151)}
    # When
    rows = await trigger_execution_dao.list_history_for_trigger(AGENT_ID, TRIGGER_ID, limit=requested)
    # Then
    assert len(rows) == expected
    assert history_raw.queries[0][1]["limit"] == expected


async def test_history_empty_page_is_read_only(history_raw: HistoryRaw) -> None:
    # Given / When
    rows = await trigger_execution_dao.list_history_for_trigger(AGENT_ID, TRIGGER_ID)
    # Then
    assert rows == []
    assert history_raw.rows == {}
    assert len(history_raw.queries) == 1
