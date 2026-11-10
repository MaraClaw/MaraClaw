from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from psycopg import AsyncConnection

from app.dao.okr_dao import ObjectivePagePosition, ObjectivePageScope, OKRObjectiveDAO
from app.db.connection import DbConnection


@pytest.fixture
def database() -> tuple[DbConnection, AsyncMock]:
    cursor = AsyncMock()
    execute = AsyncMock()
    cursor.execute = execute
    cursor.__aenter__.return_value = cursor
    cursor.fetchall.return_value = []
    raw = Mock(spec=AsyncConnection)
    raw.cursor.return_value = cursor
    return DbConnection(raw), execute


@pytest.mark.parametrize("continued", [False, True])
async def test_page_sql_is_tenant_contained_active_and_uses_unique_keyset(
    monkeypatch: pytest.MonkeyPatch, continued: bool, database: tuple[DbConnection, AsyncMock]
) -> None:
    db, execute = database

    @asynccontextmanager
    async def session() -> AsyncGenerator[DbConnection]:
        yield db

    dao = OKRObjectiveDAO()
    monkeypatch.setattr(dao, "session", session)
    scope = ObjectivePageScope(UUID(int=1), date(2026, 1, 1), date(2026, 3, 31), ("agent", "user"))
    position = ObjectivePagePosition("user", datetime(2026, 1, 2, tzinfo=UTC), UUID(int=10))

    result = await dao.list_page(scope, limit=2, after=position if continued else None)

    assert result == []
    assert execute.await_args is not None
    sql, params = execute.await_args.args
    assert "tenant_id = %(tenant_id)s" in sql
    assert "period_start >= %(period_start)s" in sql
    assert "period_end <= %(period_end)s" in sql
    assert "status <> 'archived'" in sql
    assert "owner_type = ANY(%(owner_types)s)" in sql
    assert "ORDER BY owner_type, created_at, id LIMIT %(limit)s" in sql
    expected = {
        "tenant_id": scope.tenant_id,
        "period_start": scope.period_start,
        "period_end": scope.period_end,
        "owner_types": ["agent", "user"],
        "limit": 3,
    }
    if continued:
        assert "(owner_type, created_at, id) > (%(owner_type)s, %(created_at)s, %(id)s)" in sql
        expected.update(owner_type=position.owner_type, created_at=position.created_at, id=position.id)
    else:
        assert "(owner_type, created_at, id) >" not in sql
    assert params == expected


async def test_legacy_period_query_keeps_unlimited_read(
    monkeypatch: pytest.MonkeyPatch, database: tuple[DbConnection, AsyncMock]
) -> None:
    db, execute = database

    @asynccontextmanager
    async def session() -> AsyncGenerator[DbConnection]:
        yield db

    dao = OKRObjectiveDAO()
    monkeypatch.setattr(dao, "session", session)

    await dao.list_for_period(UUID(int=1), period_start=date(2026, 1, 1), period_end=date(2026, 3, 31))

    assert execute.await_args is not None
    sql, params = execute.await_args.args
    assert "LIMIT" not in sql
    assert "limit" not in params
