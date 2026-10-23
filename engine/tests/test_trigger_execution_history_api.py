from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from tests.trigger_history_fixtures import (
    AGENT_ID,
    HISTORY_COLUMNS,
    SECRET,
    TRIGGER_ID,
    execution,
    history_raw as history_raw,
)
from tests.trigger_history_http_fixtures import HistoryHTTP, history_http as history_http

from app.core.json_types import json_object_from, json_object_from_response
from app.schemas.trigger_execution import TriggerExecutionCursor, TriggerExecutionHistoryPage


@pytest.mark.parametrize("role", ["member", "org_admin", "platform_admin"])
async def test_history_manager_and_accessible_admin_can_read(history_http: HistoryHTTP, role: str) -> None:
    # Given
    history_http.scope.user.role = role
    history_http.scope.access_level = "manage" if role == "member" else "use"
    history_http.raw.rows = {UUID(int=1): execution(1)}
    # When
    response = await history_http.client.get(history_http.url)
    # Then
    assert response.status_code == 200
    assert history_http.scope.calls == ["access", "get"]
    assert len(TriggerExecutionHistoryPage.model_validate_json(response.content).items) == 1


@pytest.mark.parametrize("denial", [403, 404])
async def test_history_agent_access_error_precedes_lookup_and_bad_cursor(
    history_http: HistoryHTTP, denial: int
) -> None:
    # Given
    history_http.scope.denied_status = denial
    # When
    response = await history_http.client.get(history_http.url, params={"cursor": SECRET * 100})
    # Then
    assert response.status_code == denial
    assert history_http.scope.calls == ["access"]
    assert not history_http.raw.queries
    assert SECRET not in response.text


async def test_history_use_only_denied_before_trigger_and_cursor_lookup(history_http: HistoryHTTP) -> None:
    # Given
    history_http.scope.access_level = "use"
    # When
    response = await history_http.client.get(history_http.url, params={"cursor": SECRET})
    # Then
    assert response.status_code == 403
    assert response.json() == {"detail": "Manage access required"}
    assert history_http.scope.calls == ["access"]
    assert not history_http.raw.queries
    assert SECRET not in response.text


@pytest.mark.parametrize("role", ["member", "org_admin"])
async def test_history_foreign_tenant_denied_before_trigger_lookup(history_http: HistoryHTTP, role: str) -> None:
    # Given
    history_http.scope.user.role = role
    history_http.scope.user.tenant_id = UUID(int=999)
    # When
    response = await history_http.client.get(history_http.url, params={"cursor": SECRET})
    # Then
    assert response.status_code == 403
    assert history_http.scope.calls == ["access"]
    assert not history_http.raw.queries
    assert SECRET not in response.text


@pytest.mark.parametrize("foreign_agent", [False, True])
async def test_history_missing_or_foreign_trigger_404_precedes_cursor(
    history_http: HistoryHTTP, foreign_agent: bool
) -> None:
    # Given
    trigger = history_http.scope.trigger
    history_http.scope.trigger = (
        replace(trigger, agent_id=UUID(int=999)) if foreign_agent else replace(trigger, id=UUID(int=999))
    )
    # When
    response = await history_http.client.get(history_http.url, params={"cursor": SECRET})
    # Then
    assert response.status_code == 404
    assert response.json() == {"detail": "Trigger not found"}
    assert history_http.scope.calls == ["access", "get"]
    assert not history_http.raw.queries
    assert SECRET not in response.text


async def test_history_requires_http_authentication(history_http: HistoryHTTP) -> None:
    # Given
    history_http.app.dependency_overrides.clear()
    # When
    response = await history_http.client.get(history_http.url, params={"cursor": SECRET})
    # Then
    assert response.status_code == 401
    assert history_http.scope.calls == []
    assert not history_http.raw.queries
    assert SECRET not in response.text


async def test_history_serializes_only_safe_stored_fields_without_side_effects(history_http: HistoryHTTP) -> None:
    # Given
    row = replace(execution(1), source="new-source", status="new-status", started_at=datetime(2026, 10, 23, tzinfo=UTC))
    history_http.raw.rows = {row.id: row, UUID(int=2): replace(execution(2), agent_id=UUID(int=999))}
    original = asdict(row), asdict(history_http.scope.trigger)
    # When
    response = await history_http.client.get(history_http.url)
    # Then
    assert response.status_code == 200
    body = json_object_from_response(response)
    assert set(body) == {"items", "next_cursor"}
    items = body["items"]
    assert isinstance(items, list)
    assert set(json_object_from(items[0])) == set(HISTORY_COLUMNS)
    item = TriggerExecutionHistoryPage.model_validate_json(response.content).items[0]
    assert (item.id, item.trigger_id, item.agent_id) == (row.id, TRIGGER_ID, AGENT_ID)
    assert (item.source, item.status) == ("new-source", "new-status")
    assert (item.scheduled_at, item.created_at, item.started_at, item.finished_at) == (
        row.scheduled_at,
        row.created_at,
        row.started_at,
        None,
    )
    assert SECRET not in response.text
    assert original == (asdict(row), asdict(history_http.scope.trigger))
    assert not history_http.scope.updates
    assert not history_http.scope.deleted


@pytest.mark.parametrize("count", [0, 20, 21, 100, 101])
async def test_history_page_boundary_default_and_max_limit(history_http: HistoryHTTP, count: int) -> None:
    # Given
    history_http.raw.rows = {UUID(int=i): execution(i) for i in range(1, count + 1)}
    limit = 100 if count >= 100 else 20
    params = {"limit": "100"} if count >= 100 else {}
    # When
    response = await history_http.client.get(history_http.url, params=params)
    # Then
    assert response.status_code == 200
    page = TriggerExecutionHistoryPage.model_validate_json(response.content)
    assert [item.id.int for item in page.items] == list(range(count, max(count - limit, 0), -1))
    assert history_http.raw.queries[0][1]["limit"] == limit + 1
    if count > limit:
        assert page.next_cursor is not None
        assert "=" not in page.next_cursor
        position = TriggerExecutionCursor.decode(page.next_cursor, AGENT_ID, TRIGGER_ID)
        assert position.id == page.items[-1].id
        assert position.created_at == page.items[-1].created_at
    else:
        assert page.next_cursor is None


async def test_history_http_cursor_continuation_after_insert_and_status_change(history_http: HistoryHTTP) -> None:
    # Given
    history_http.raw.rows = {UUID(int=i): execution(i) for i in range(1, 5)}
    first_response = await history_http.client.get(history_http.url, params={"limit": 2})
    first = TriggerExecutionHistoryPage.model_validate_json(first_response.content)
    assert first.next_cursor is not None
    history_http.raw.rows[UUID(int=5)] = execution(5)
    history_http.raw.rows[UUID(int=2)].status = "done"
    # When
    response = await history_http.client.get(history_http.url, params={"limit": 2, "cursor": first.next_cursor})
    # Then
    assert response.status_code == 200
    page = TriggerExecutionHistoryPage.model_validate_json(response.content)
    assert [item.id.int for item in page.items] == [2, 1]
    assert page.items[0].status == "done"
    assert page.next_cursor is None


@pytest.mark.parametrize("limit", ["0", "101", "-1", "not-an-int"])
async def test_history_invalid_limit_rejected_without_execution_query(history_http: HistoryHTTP, limit: str) -> None:
    # Given / When
    response = await history_http.client.get(history_http.url, params={"limit": limit})
    # Then
    assert response.status_code == 422
    assert not history_http.raw.queries
    assert SECRET not in response.text
