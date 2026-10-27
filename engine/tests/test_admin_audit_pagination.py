import json
from base64 import b64decode, urlsafe_b64encode
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.core.json_types import json_loads_object
from app.dao.admin_audit_dao import admin_audit_log_dao
from app.records.audit import AdminAuditLogRecord
from app.services.admin_audit_pagination import AdminAuditScope, list_admin_audit_page

_NOW = datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC)
_SCOPE = AdminAuditScope(role="platform_admin", tenant_id=uuid4(), actor_id=uuid4(), action="tenant_create")


@pytest.fixture
def rows() -> list[AdminAuditLogRecord]:
    return [
        AdminAuditLogRecord(
            id=UUID(int=number),
            actor_id=_SCOPE.actor_id,
            actor_role="platform_admin",
            actor_email="admin@example.test",
            action="tenant_create",
            target_type="tenant",
            target_id=_SCOPE.tenant_id,
            tenant_id=_SCOPE.tenant_id,
            created_at=_NOW,
        )
        for number in (3, 2, 1)
    ]


async def test_cursor_uses_last_visible_row_and_preserves_microseconds(monkeypatch, rows):
    # Given a full page with one lookahead row at the same timestamp.
    query = AsyncMock(side_effect=[rows, rows[2:]])
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    first = await list_admin_audit_page(_SCOPE, limit=2)
    assert first.next_cursor is not None

    # When the next page is requested.
    older = await list_admin_audit_page(_SCOPE, cursor=first.next_cursor, limit=2)

    # Then the boundary excludes neither a tied row nor microsecond precision.
    assert [item.id for item in first.items] == [UUID(int=3), UUID(int=2)]
    assert query.await_args is not None
    assert query.await_args.kwargs["before"] == (_NOW, UUID(int=2))
    assert [item.id for item in older.items] == [UUID(int=1)]
    assert older.next_cursor is None


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_empty_partial_and_exact_full_pages_are_exhausted(monkeypatch, rows, count):
    # Given at most the requested number of rows.
    monkeypatch.setattr(admin_audit_log_dao, "list_page", AsyncMock(return_value=rows[:count]))
    # When a page is read.
    page = await list_admin_audit_page(_SCOPE, limit=2)
    # Then only actual lookahead can produce a next cursor.
    assert len(page.items) == count
    assert page.next_cursor is None


@pytest.mark.parametrize(("requested", "effective"), [(-1, 1), (0, 1), (1, 1), (100, 100), (500, 500), (501, 500)])
async def test_page_limit_retains_existing_clamping(monkeypatch, requested, effective):
    # Given a bounded query seam.
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    # When a caller supplies any existing limit value.
    await list_admin_audit_page(_SCOPE, limit=requested)
    # Then the same 1..500 clamp reaches the DAO.
    assert query.await_args is not None
    assert query.await_args.kwargs["limit"] == effective


@pytest.mark.parametrize("cursor", ["", "!", "x" * 1025, "💥", "bnVsbA", "W10", "bm90LWpzb24", "e30"])
async def test_malformed_cursor_fails_before_query(monkeypatch, cursor):
    # Given malformed, oversized or structurally invalid cursor input.
    query = AsyncMock()
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    # When an authorized reader supplies it.
    with pytest.raises(HTTPException) as exc:
        await list_admin_audit_page(_SCOPE, cursor=cursor)
    # Then all cursor parse failures are 400 and cannot issue SQL.
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid cursor"
    query.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "org_admin"},
        {"tenant_id": uuid4()},
        {"tenant_id": None},
        {"actor_id": uuid4()},
        {"actor_id": None},
        {"action": "tenant_delete"},
        {"action": None},
    ],
)
async def test_cursor_is_bound_to_effective_role_tenant_actor_and_action(monkeypatch, rows, changes):
    # Given a cursor issued for one effective query scope.
    query = AsyncMock(return_value=rows)
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    page = await list_admin_audit_page(_SCOPE, limit=2)
    query.reset_mock()
    # When any material scope changes.
    with pytest.raises(HTTPException) as exc:
        await list_admin_audit_page(replace(_SCOPE, **changes), cursor=page.next_cursor)
    # Then it is rejected before querying the new scope.
    assert exc.value.status_code == 400
    query.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 0),
        ("version", 1),
        ("version", True),
        ("version", "1"),
        ("created_at", "2026-10-24T12:00:00.123456"),
        ("created_at", None),
        ("id", "not-a-uuid"),
        ("extra", "forbidden"),
    ],
)
async def test_cursor_schema_is_strict_and_versioned(monkeypatch, rows, field, value):
    # Given a valid issued cursor with one corrupted field.
    query = AsyncMock(return_value=rows)
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    page = await list_admin_audit_page(_SCOPE, limit=2)
    assert page.next_cursor is not None
    payload = json_loads_object(b64decode(page.next_cursor + "=" * (-len(page.next_cursor) % 4), altchars=b"-_"))
    assert payload["version"] == 2
    payload[field] = value
    cursor = urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    query.reset_mock()
    # When the corrupted cursor is presented.
    with pytest.raises(HTTPException) as exc:
        await list_admin_audit_page(_SCOPE, cursor=cursor)
    # Then coercion and unknown fields cannot bypass version/scope validation.
    assert exc.value.status_code == 400
    query.assert_not_awaited()


async def test_absent_and_empty_action_have_the_same_effective_scope(monkeypatch, rows):
    # Given no effective action filter.
    monkeypatch.setattr(admin_audit_log_dao, "list_page", AsyncMock(return_value=rows))
    page = await list_admin_audit_page(replace(_SCOPE, action=None), limit=2)
    # When the next request uses the equivalent empty filter and another page size.
    result = await list_admin_audit_page(replace(_SCOPE, action=""), cursor=page.next_cursor, limit=3)
    # Then the cursor remains valid and page size is not a scope filter.
    assert len(result.items) == 3


@pytest.mark.parametrize("field", ["start_at", "end_at"])
@pytest.mark.parametrize("transition", ["add", "remove", "change"])
async def test_cursor_rejects_changed_or_absent_window_bounds(monkeypatch, rows, field, transition):
    # Given a cursor with an explicit bounded or unbounded interval.
    start, end = _NOW - timedelta(days=1), _NOW + timedelta(days=1)
    scope = replace(_SCOPE, start_at=start, end_at=end)
    if transition == "add":
        scope = replace(scope, **{field: None})
    query = AsyncMock(return_value=rows)
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    page = await list_admin_audit_page(scope, limit=2)
    changed = None if transition == "remove" else _NOW
    query.reset_mock()
    # When either bound changes, including its presence.
    with pytest.raises(HTTPException) as exc:
        await list_admin_audit_page(replace(scope, **{field: changed}), cursor=page.next_cursor)
    # Then the cursor cannot silently switch windows.
    assert exc.value.status_code == 400
    query.assert_not_awaited()


@pytest.mark.parametrize("start_at", [None, _NOW - timedelta(days=1)])
@pytest.mark.parametrize("end_at", [None, _NOW + timedelta(days=1)])
async def test_dao_parameterizes_present_inclusive_start_and_exclusive_end(monkeypatch, start_at, end_at):
    # Given a connection seam that captures the actual DAO statement.
    queries: list[tuple[str, dict[str, object]]] = []

    class Database:
        async def fetchall(self, sql: str, params: dict[str, object]) -> list[dict[str, object]]:
            queries.append((sql, params))
            return []

    @asynccontextmanager
    async def session():
        yield Database()

    monkeypatch.setattr(admin_audit_log_dao, "session", session)
    # When any combination of optional bounds is queried.
    await admin_audit_log_dao.list_page(start_at=start_at, end_at=end_at)
    # Then date values are parameters, not SQL literals, with half-open semantics.
    [(sql, params)] = queries
    assert ("created_at >= %(start_at)s" in sql) == (start_at is not None)
    assert ("created_at < %(end_at)s" in sql) == (end_at is not None)
    assert params.get("start_at") == start_at
    assert params.get("end_at") == end_at
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert "OFFSET" not in sql
