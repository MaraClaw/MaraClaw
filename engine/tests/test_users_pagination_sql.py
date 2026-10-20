from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.dao.agent_dao import agent_dao
from app.dao.user_dao import UserPagePosition, user_dao

TENANT = UUID(int=100)


class _QueryConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, Mapping[str, object]]] = []
        self.rows: list[dict[str, object]] = []

    async def fetchall(self, query: str, params: Mapping[str, object]) -> list[dict[str, object]]:
        self.queries.append((query, params))
        return self.rows


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> _QueryConnection:
    connection = _QueryConnection()

    @asynccontextmanager
    async def session() -> AsyncGenerator[_QueryConnection]:
        yield connection

    monkeypatch.setattr(user_dao, "session", session)
    monkeypatch.setattr(agent_dao, "session", session)
    return connection


async def test_limits_sql_when_fetching_identity_backed_page(connection: _QueryConnection):
    result = await user_dao.list_for_tenant_page(TENANT, limit=100)
    assert result == []
    assert len(connection.queries) == 1
    query, params = connection.queries[0]
    assert params == {"tenant_id": TENANT, "limit": 101}
    assert "WHERE u.tenant_id = %(tenant_id)s" in query
    assert "LEFT JOIN identities i ON i.id = u.identity_id" in query
    assert "i.must_change_password AS i_must_change_password" in query
    assert "CASE WHEN u.role IN ('member', 'agent_admin') THEN 1 ELSE 0 END ASC" in query
    assert "u.created_at ASC NULLS LAST, u.id ASC LIMIT %(limit)s" in query
    assert "OFFSET" not in query


@pytest.mark.parametrize("search", ["%", "_", "\\", "A%_\\' OR TRUE --"])
async def test_escapes_wildcards_when_searching_literal_substring(connection: _QueryConnection, search: str):
    await user_dao.list_for_tenant_page(TENANT, search=search)
    query, params = connection.queries[0]
    expected = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    assert params["search"] == expected
    for column in ("u.display_name", "i.email", "i.username"):
        assert f"{column} ILIKE %(search)s ESCAPE E'\\\\'" in query
    assert "WHERE u.tenant_id = %(tenant_id)s AND (" in query


@pytest.mark.parametrize("created_at", [None, datetime(2026, 10, 20, tzinfo=UTC)])
async def test_keyset_when_timestamp_is_tied_or_null(connection: _QueryConnection, created_at: datetime | None):
    position = UserPagePosition(1, created_at, UUID(int=5))
    await user_dao.list_for_tenant_page(TENANT, after=position, limit=1)
    query, params = connection.queries[0]
    assert params["role_rank"] == 1
    assert params["user_id"] == UUID(int=5)
    assert params["limit"] == 2
    assert "END > %(role_rank)s OR (" in query
    assert "END = %(role_rank)s AND (" in query
    if created_at is None:
        assert "u.created_at IS NULL AND u.id > %(user_id)s" in query
        assert "created_at" not in params
    else:
        assert params["created_at"] == created_at
        assert "u.created_at IS NULL OR u.created_at > %(created_at)s" in query
        assert "u.created_at = %(created_at)s AND u.id > %(user_id)s" in query


async def test_preserves_identity_flags_when_projecting_joined_rows(connection: _QueryConnection):
    connection.rows = [
        {
            "id": UUID(int=5),
            "tenant_id": TENANT,
            "role": "org_admin",
            "registration_source": "web",
            "i_id": UUID(int=6),
            "i_email": "alice@example.com",
            "i_username": "alice",
            "i_must_change_password": True,
            "i_is_platform_admin": True,
        }
    ]
    result = await user_dao.list_for_tenant_page(TENANT)
    assert [(user.email, user.username, user.must_change_password, user.is_platform_admin) for user in result] == [
        ("alice@example.com", "alice", True, True)
    ]


async def test_batches_active_counts_when_page_has_multiple_creators(connection: _QueryConnection):
    first, second = UUID(int=1), UUID(int=2)
    connection.rows = [{"creator_id": first, "count": 3}, {"creator_id": second, "count": 1}]
    counts = await agent_dao.count_active_for_creators([first, second])
    assert counts == {first: 3, second: 1}
    assert len(connection.queries) == 1
    query, params = connection.queries[0]
    assert params == {"creator_ids": [first, second]}
    assert "creator_id = ANY(%(creator_ids)s) AND is_expired IS FALSE GROUP BY creator_id" in query
    assert "status" not in query


async def test_skips_count_query_when_page_is_empty(connection: _QueryConnection):
    counts = await agent_dao.count_active_for_creators([])
    assert counts == {}
    assert connection.queries == []
