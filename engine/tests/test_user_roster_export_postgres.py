import csv
from collections.abc import Sequence
from io import StringIO
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

import user_roster_export_fixtures as fixtures
from app.api.users import UserRosterExportOut
from app.dao.agent_dao import agent_dao
from app.dao.user_dao import UserPagePosition, user_dao
from app.db.connection import DbConnection
from app.db.session import get_connection
from app.records.user import UserRecord
from user_roster_export_fixtures import (
    FOREIGN,
    NOW,
    TENANT,
    RosterDatabase,
    seed_agent,
    seed_matches,
)

roster_db = fixtures.roster_db
roster_client = fixtures.roster_client


def _rows(response: httpx.Response) -> list[list[str]]:
    assert response.status_code == 200
    return list(csv.reader(StringIO(UserRosterExportOut.model_validate_json(response.content).csv, newline="")))


@pytest.mark.parametrize("count", [0, 99, 100, 101, 200, 201])
async def test_complete_ordered_literal_export_when_matches_cross_page_boundaries(
    roster_db: RosterDatabase, roster_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, count: int
):
    await seed_matches(roster_db, count)
    await roster_db.writer.execute(
        "INSERT INTO users (id, tenant_id, display_name, role) VALUES "
        "(%(company_pa)s, %(tenant)s, 'Match%%_\\ PA', 'platform_admin'), "
        "(%(separate_pa)s, NULL, 'Match%%_\\ Separate', 'platform_admin'), "
        "(%(foreign)s, %(foreign_tenant)s, 'Match%%_\\ Foreign', 'member'), "
        "(%(wildcard)s, %(tenant)s, 'MatchXYZ wildcard', 'member')",
        {
            "company_pa": UUID(int=4),
            "separate_pa": UUID(int=3),
            "foreign": UUID(int=2),
            "tenant": TENANT,
            "foreign_tenant": FOREIGN,
            "wildcard": UUID(int=5),
        },
    )
    per_user_count = AsyncMock(side_effect=AssertionError("export must batch counts"))
    monkeypatch.setattr(agent_dao, "count_active_for_creator", per_user_count)

    response = await roster_client.get(
        "/api/users/export", params={"search": "  mAtCh%_\\  ", "tenant_id": str(FOREIGN)}
    )

    rows = _rows(response)
    assert rows[0] == ["id", "email", "display_name", "role", "is_active", "agents_count", "created_at", "source"]
    expected = [*range(10, 10 + min(count, 2)), 4, *range(12, 10 + count)]
    assert [UUID(row[0]).int for row in rows[1:]] == expected
    assert all(len(row) == 8 for row in rows)
    per_user_count.assert_not_awaited()
    audit = await roster_db.writer.fetchone("SELECT action, details, changes, tenant_id FROM admin_audit_logs")
    assert audit == {
        "action": "user_roster_export_generated",
        "details": {"rows_count": count + 1, "search_applied": True},
        "changes": {},
        "tenant_id": TENANT,
    }


async def test_empty_csv_when_company_has_no_search_matches(roster_client: httpx.AsyncClient):
    rows = _rows(await roster_client.get("/api/users/export", params={"search": "not found"}))
    assert len(rows) == 1


async def test_quotes_public_fields_when_text_contains_csv_syntax_and_formula_prefixes(
    roster_db: RosterDatabase, roster_client: httpx.AsyncClient
):
    await roster_db.writer.execute(
        "INSERT INTO identities (id, email, password_hash) VALUES (%(id)s, %(email)s, 'secret-never-export')",
        {"id": UUID(int=10), "email": '  =HYPERLINK("evil,site")'},
    )
    await roster_db.writer.execute(
        "INSERT INTO users (id, identity_id, tenant_id, display_name, role, is_active, registration_source, created_at) "
        "VALUES (%(id)s, %(id)s, %(tenant)s, %(name)s, 'member', false, %(source)s, %(created)s)",
        {"id": UUID(int=10), "tenant": TENANT, "name": '  普通, "quoted"\r\nname  ', "source": "\tSSO", "created": NOW},
    )
    await seed_agent(roster_db, 10)
    await seed_agent(roster_db, 10, expired=True)
    await seed_agent(roster_db, 10, tenant=FOREIGN)

    rows = _rows(await roster_client.get("/api/users/export", params={"search": "普通"}))

    assert rows[1:] == [
        [
            str(UUID(int=10)),
            '\'  =HYPERLINK("evil,site")',
            '  普通, "quoted"\r\nname  ',
            "member",
            "false",
            "1",
            NOW.isoformat(),
            "'\tSSO",
        ]
    ]


async def test_snapshot_includes_initial_roster_identity_and_counts_when_other_connection_commits(
    roster_db: RosterDatabase, roster_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    await seed_matches(roster_db, 201)
    await seed_agent(roster_db, 10)
    await seed_agent(roster_db, 110)
    await roster_db.writer.execute(
        "INSERT INTO identities (id, email) VALUES (%(id)s, 'before@example.com')",
        {"id": UUID(int=110)},
    )
    await roster_db.writer.execute("UPDATE users SET identity_id=%(id)s WHERE id=%(id)s", {"id": UUID(int=110)})
    original_page = user_dao.list_for_tenant_page
    original_counts = agent_dao.count_active_for_creators
    positions: list[UserPagePosition | None] = []
    snapshot_connections: list[DbConnection | None] = []
    count_batches: list[list[UUID]] = []

    async def page(
        tenant_id: UUID, *, search: str = "", after: UserPagePosition | None = None, limit: int = 50
    ) -> Sequence[UserRecord]:
        positions.append(after)
        snapshot_connections.append(get_connection())
        result = await original_page(tenant_id, search=search, after=after, limit=limit)
        if len(positions) == 1:
            await roster_db.writer.execute("UPDATE agents SET is_expired=true")
            await roster_db.writer.execute(
                "UPDATE users SET display_name='Changed', role='org_admin', created_at=NULL WHERE id=%(id)s",
                {"id": UUID(int=110)},
            )
            await roster_db.writer.execute("UPDATE identities SET email='after@example.com'")
            await roster_db.writer.execute("DELETE FROM users WHERE id=%(id)s", {"id": UUID(int=210)})
            await roster_db.writer.execute(
                "INSERT INTO users (id, tenant_id, display_name, role) VALUES (%(id)s, %(tenant)s, 'Match%%_\\ New', 'member')",
                {"id": UUID(int=500), "tenant": TENANT},
            )
        return result

    async def counts(ids: Sequence[UUID]) -> dict[UUID, int]:
        snapshot_connections.append(get_connection())
        count_batches.append(list(ids))
        return await original_counts(ids)

    monkeypatch.setattr(user_dao, "list_for_tenant_page", page)
    monkeypatch.setattr(agent_dao, "count_active_for_creators", counts)

    rows = _rows(await roster_client.get("/api/users/export", params={"search": "Match%_\\"}))[1:]

    assert [UUID(row[0]).int for row in rows] == list(range(10, 211))
    assert rows[100][1:4] == ["before@example.com", "Match%_\\ 100", "member"]
    assert {UUID(row[0]).int: int(row[5]) for row in rows if int(row[5])} == {10: 1, 110: 1}
    assert [len(ids) for ids in count_batches] == [100, 100, 1]
    assert positions == [None, UserPagePosition(1, NOW, UUID(int=109)), UserPagePosition(1, None, UUID(int=209))]
    assert snapshot_connections[0] is not None
    assert all(connection is snapshot_connections[0] for connection in snapshot_connections)
    assert get_connection() is None
