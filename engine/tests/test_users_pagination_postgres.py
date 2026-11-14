import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg.rows import DictRow, dict_row

from app.api import users as users_api
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.organization_admission_dao import organization_admission_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.records.identity import IdentityRecord
from app.records.user import UserRecord

TENANT = UUID(int=100)
FOREIGN = UUID(int=200)
NOW = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.fixture
async def database(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[DbConnection]:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN")
    if dsn is None or dsn == "":
        pytest.skip("Set MARACLAW_TEST_POSTGRES_DSN to run isolated temporary-table Postgres checks")
    async with await psycopg.AsyncConnection[DictRow].connect(dsn, row_factory=dict_row, autocommit=True) as raw:
        await raw.execute("""
            CREATE TEMP TABLE identities (
                id uuid PRIMARY KEY, email text, phone text, password_hash text,
                is_active boolean DEFAULT true, is_platform_admin boolean DEFAULT false,
                email_verified boolean DEFAULT false, must_change_password boolean DEFAULT false,
                created_at timestamptz, updated_at timestamptz
            );
            CREATE TEMP TABLE users (
                id uuid PRIMARY KEY, identity_id uuid, tenant_id uuid, display_name text,
                avatar_url text, title text, timezone text, role text NOT NULL, is_active boolean DEFAULT true,
                registration_source text DEFAULT 'web', created_at timestamptz, updated_at timestamptz,
                quota_message_limit integer DEFAULT 50, quota_message_period text DEFAULT 'permanent',
                quota_messages_used integer DEFAULT 0, quota_period_start timestamptz,
                quota_max_agents integer DEFAULT 2, quota_agent_ttl_hours integer DEFAULT 0,
                is_genesis boolean DEFAULT false
            );
            CREATE TEMP TABLE agents (creator_id uuid, tenant_id uuid, is_expired boolean, status text);
            CREATE TEMP TABLE organization_admission_requests (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tier text NOT NULL, status text NOT NULL,
                provisioned_tenant_id uuid, provisioned_at timestamptz DEFAULT now()
            );
        """)
        connection = DbConnection(raw)

        @asynccontextmanager
        async def session() -> AsyncGenerator[DbConnection]:
            yield connection

        monkeypatch.setattr(user_dao, "session", session)
        monkeypatch.setattr(agent_dao, "session", session)
        monkeypatch.setattr(organization_admission_dao, "session", session)
        yield connection


async def _insert(connection: DbConnection, user: UserRecord) -> None:
    if user.identity is not None:
        await connection.execute(
            "INSERT INTO identities (id, email, must_change_password) "
            "VALUES (%(id)s, %(email)s, %(must_change_password)s)",
            {
                "id": user.identity.id,
                "email": user.email,
                "must_change_password": user.must_change_password,
            },
        )
    await connection.execute(
        "INSERT INTO users (id, identity_id, tenant_id, display_name, role, created_at) "
        "VALUES (%(id)s, %(identity_id)s, %(tenant_id)s, %(display_name)s, %(role)s, %(created_at)s)",
        {
            "id": user.id,
            "identity_id": user.identity.id if user.identity else None,
            "tenant_id": user.tenant_id,
            "display_name": user.display_name,
            "role": user.role,
            "created_at": user.created_at,
        },
    )


@pytest.fixture
async def client(database: DbConnection) -> AsyncGenerator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: UserRecord(
        id=UUID(int=1), tenant_id=TENANT, role=UserRole.ORG_ADMIN
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def directory(database: DbConnection) -> None:
    rows = [
        UserRecord(id=UUID(int=10), tenant_id=TENANT, role=UserRole.ORG_ADMIN, created_at=NOW + timedelta(days=2)),
        UserRecord(id=UUID(int=11), tenant_id=TENANT, role=UserRole.PLATFORM_ADMIN, created_at=NOW + timedelta(days=2)),
        UserRecord(id=UUID(int=12), tenant_id=TENANT, role=UserRole.ORG_ADMIN, created_at=None),
        UserRecord(id=UUID(int=20), tenant_id=TENANT, role=UserRole.MEMBER, created_at=NOW),
        UserRecord(id=UUID(int=21), tenant_id=TENANT, role=UserRole.AGENT_ADMIN, created_at=NOW),
        UserRecord(id=UUID(int=22), tenant_id=TENANT, role=UserRole.MEMBER, created_at=NOW + timedelta(days=1)),
        UserRecord(id=UUID(int=23), tenant_id=TENANT, role=UserRole.MEMBER, created_at=None),
        UserRecord(id=UUID(int=24), tenant_id=TENANT, role=UserRole.AGENT_ADMIN, created_at=None),
        UserRecord(id=UUID(int=30), tenant_id=FOREIGN, role=UserRole.ORG_ADMIN, created_at=NOW),
        UserRecord(id=UUID(int=31), tenant_id=FOREIGN, role=UserRole.MEMBER, created_at=None),
        UserRecord(id=UUID(int=40), tenant_id=None, role=UserRole.PLATFORM_ADMIN, created_at=None),
    ]
    for row in reversed(rows):
        await _insert(database, row)


@pytest.mark.parametrize("limit", [1, 2, 3, 100])
async def test_exhausts_in_stable_order_when_roles_timestamps_and_nulls_tie(
    client: httpx.AsyncClient, directory: None, limit: int
):
    ids: list[int] = []
    cursor = None
    for _page in range(9):
        params = {"limit": str(limit), "tenant_id": str(FOREIGN)}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/api/users/page", params=params)
        assert response.status_code == 200
        page = users_api.UsersPageOut.model_validate_json(response.content)
        assert len(page.items) <= limit
        ids.extend(item.id.int for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert cursor is None
    assert ids == [10, 11, 12, 20, 21, 22, 23, 24]


@pytest.mark.parametrize("search", ["nAmE%_\\", "mAiL%_\\@", "%", "_", "\\", "' OR TRUE --"])
async def test_literal_identity_search_when_wildcards_and_foreign_matches_exist(
    client: httpx.AsyncClient, database: DbConnection, search: str
):
    for identifier, tenant in [(10, TENANT), (12, FOREIGN)]:
        await _insert(
            database,
            UserRecord(
                id=UUID(int=identifier),
                tenant_id=tenant,
                role=UserRole.MEMBER,
                display_name="Name%_\\MiXeD",
                identity=IdentityRecord(id=UUID(int=identifier), email="Mail%_\\@example.com"),
            ),
        )
    await _insert(
        database,
        UserRecord(
            id=UUID(int=11),
            tenant_id=TENANT,
            role=UserRole.MEMBER,
            display_name="NameXYZmixed",
            identity=IdentityRecord(id=UUID(int=11), email="MailXYZ@example.com"),
        ),
    )
    await _insert(database, UserRecord(id=UUID(int=13), tenant_id=TENANT, role=UserRole.MEMBER))
    await database.execute("UPDATE users SET display_name = NULL WHERE id = %(id)s", {"id": UUID(int=13)})
    response = await client.get("/api/users/page", params={"search": search})
    assert response.status_code == 200
    page = users_api.UsersPageOut.model_validate_json(response.content)
    assert [item.id.int for item in page.items] == ([] if search == "' OR TRUE --" else [10])


async def test_preserves_remaining_rows_when_new_users_are_inserted_before_and_after_cursor(
    client: httpx.AsyncClient, database: DbConnection, directory: None
):
    first_response = await client.get("/api/users/page", params={"limit": 5})
    first = users_api.UsersPageOut.model_validate_json(first_response.content)
    assert [item.id.int for item in first.items] == [10, 11, 12, 20, 21]
    assert first.next_cursor is not None
    await _insert(database, UserRecord(id=UUID(int=9), tenant_id=TENANT, role=UserRole.ORG_ADMIN, created_at=NOW))
    await _insert(database, UserRecord(id=UUID(int=19), tenant_id=TENANT, role=UserRole.MEMBER, created_at=NOW))
    await _insert(database, UserRecord(id=UUID(int=25), tenant_id=TENANT, role=UserRole.MEMBER, created_at=NOW))
    response = await client.get("/api/users/page", params={"limit": 100, "cursor": first.next_cursor})
    assert response.status_code == 200
    page = users_api.UsersPageOut.model_validate_json(response.content)
    assert [item.id.int for item in page.items] == [25, 22, 23, 24]
    assert page.next_cursor is None


async def test_counts_nonexpired_agents_when_returned_page_has_creators_without_agents(
    client: httpx.AsyncClient, database: DbConnection, directory: None
):
    for creator, expired, status in [
        (10, False, "running"),
        (10, False, "stopped"),
        (10, True, "running"),
        (12, False, "running"),
        (30, False, "running"),
    ]:
        await database.execute(
            "INSERT INTO agents (creator_id, tenant_id, is_expired, status) "
            "VALUES (%(creator)s, %(tenant)s, %(expired)s, %(status)s)",
            {
                "creator": UUID(int=creator),
                "tenant": FOREIGN if creator == 30 else TENANT,
                "expired": expired,
                "status": status,
            },
        )
    response = await client.get("/api/users/page", params={"limit": 2})
    assert response.status_code == 200
    page = users_api.UsersPageOut.model_validate_json(response.content)
    assert [(item.id.int, item.agents_count) for item in page.items] == [(10, 2), (11, 0)]


async def test_reports_the_tier_cap_as_effective_limit_only_for_the_linked_tenant(
    client: httpx.AsyncClient, database: DbConnection, directory: None
):
    before = await client.get("/api/users/page", params={"limit": 100})
    page = users_api.UsersPageOut.model_validate_json(before.content)
    assert {item.id.int: item.effective_max_agents for item in page.items} == {
        10: None,
        11: None,
        12: None,
        20: 2,
        21: 2,
        22: 2,
        23: 2,
        24: 2,
    }
    await database.execute(
        "INSERT INTO organization_admission_requests (tier, status, provisioned_tenant_id) "
        "VALUES ('ultra', 'approved', %(tenant)s), ('pro', 'approved', %(foreign)s)",
        {"tenant": TENANT, "foreign": FOREIGN},
    )
    after = await client.get("/api/users/page", params={"limit": 100})
    page = users_api.UsersPageOut.model_validate_json(after.content)
    assert {item.effective_max_agents for item in page.items} == {5}


async def test_joins_identity_when_search_match_lies_beyond_first_page(
    client: httpx.AsyncClient, database: DbConnection, directory: None
):
    await _insert(
        database,
        UserRecord(
            id=UUID(int=50),
            tenant_id=TENANT,
            role=UserRole.MEMBER,
            created_at=None,
            identity=IdentityRecord(id=UUID(int=50), email="Target@example.com", must_change_password=True),
        ),
    )
    response = await client.get("/api/users/page", params={"search": "TARGET@", "limit": 1})
    assert response.status_code == 200
    page = users_api.UsersPageOut.model_validate_json(response.content)
    assert [(item.id.int, item.email) for item in page.items] == [(50, "Target@example.com")]
    assert page.next_cursor is None


async def _eligibility_directory(database: DbConnection) -> None:
    def identified(number: int, role: UserRole, created_at: datetime | None, name: str) -> UserRecord:
        return UserRecord(
            id=UUID(int=number),
            tenant_id=TENANT,
            role=role,
            created_at=created_at,
            display_name=name,
            identity=IdentityRecord(id=UUID(int=number), email=f"{name}@example.com"),
        )

    rows = [
        identified(10, UserRole.ORG_ADMIN, NOW, "admin"),
        identified(11, UserRole.PLATFORM_ADMIN, NOW, "platform"),
        identified(20, UserRole.MEMBER, NOW, "alpha"),
        identified(21, UserRole.AGENT_ADMIN, NOW, "bravo"),
        identified(22, UserRole.MEMBER, None, "charlie"),
        identified(23, UserRole.MEMBER, NOW, "inactive-membership"),
        identified(24, UserRole.AGENT_ADMIN, NOW, "inactive-identity"),
        identified(25, UserRole.MEMBER, NOW, "source"),
        UserRecord(id=UUID(int=26), tenant_id=TENANT, role=UserRole.MEMBER, created_at=NOW, display_name="no-identity"),
        identified(30, UserRole.MEMBER, NOW, "foreign"),
    ]
    for row in rows:
        await _insert(database, row)
    await database.execute(
        "UPDATE users SET tenant_id = %(foreign)s WHERE id = %(id)s", {"foreign": FOREIGN, "id": UUID(int=30)}
    )
    await database.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": UUID(int=23)})
    await database.execute("UPDATE identities SET is_active = false WHERE id = %(id)s", {"id": UUID(int=24)})


async def _exhaust(client: httpx.AsyncClient, params: dict[str, str]) -> list[int]:
    ids: list[int] = []
    cursor = None
    for _page in range(12):
        response = await client.get(
            "/api/users/page", params={**params, **({} if cursor is None else {"cursor": cursor})}
        )
        assert response.status_code == 200
        page = users_api.UsersPageOut.model_validate_json(response.content)
        ids.extend(item.id.int for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            return ids
    raise AssertionError("cursor chain did not terminate")


@pytest.mark.parametrize("limit", ["1", "2", "100"])
async def test_lists_only_transferable_users_when_admins_inactive_and_identityless_rows_exist(
    client: httpx.AsyncClient, database: DbConnection, limit: str
):
    await _eligibility_directory(database)
    ids = await _exhaust(
        client, {"eligible_for": "agent_transfer", "exclude_user_id": str(UUID(int=25)), "limit": limit}
    )
    assert ids == [20, 21, 22]
    unfiltered = await _exhaust(client, {"limit": "100"})
    assert {23, 24, 25, 26, 10, 11} <= set(unfiltered)


async def test_orders_eligible_users_like_the_unfiltered_page(client: httpx.AsyncClient, database: DbConnection):
    await _eligibility_directory(database)
    filtered = await _exhaust(client, {"eligible_for": "agent_transfer", "limit": "2"})
    unfiltered = await _exhaust(client, {"limit": "100"})
    assert filtered == [uid for uid in unfiltered if uid in {20, 21, 22, 25}]
    assert 25 in filtered
    assert not {10, 11, 23, 24, 26, 30} & set(filtered)


async def test_excludes_source_without_eligibility_when_only_exclusion_is_requested(
    client: httpx.AsyncClient, database: DbConnection
):
    await _eligibility_directory(database)
    ids = await _exhaust(client, {"exclude_user_id": str(UUID(int=10)), "limit": "100"})
    assert 10 not in ids
    assert {11, 23, 24, 25, 26} <= set(ids)


async def test_filters_before_search_when_eligible_user_matches_beyond_first_page(
    client: httpx.AsyncClient, database: DbConnection
):
    await _eligibility_directory(database)
    ids = await _exhaust(client, {"eligible_for": "agent_transfer", "search": "EXAMPLE.com", "limit": "1"})
    assert sorted(ids) == [20, 21, 22, 25]
    ids = await _exhaust(client, {"eligible_for": "agent_transfer", "search": "inactive", "limit": "1"})
    assert ids == []


async def test_rejects_cursor_when_filters_change_between_pages(client: httpx.AsyncClient, database: DbConnection):
    await _eligibility_directory(database)
    params = {"eligible_for": "agent_transfer", "limit": "1"}
    first = users_api.UsersPageOut.model_validate_json((await client.get("/api/users/page", params=params)).content)
    assert first.next_cursor is not None
    unfiltered = await client.get("/api/users/page", params={"limit": "1", "cursor": first.next_cursor})
    assert unfiltered.status_code == 400
    other = await client.get(
        "/api/users/page", params={**params, "exclude_user_id": str(UUID(int=25)), "cursor": first.next_cursor}
    )
    assert other.status_code == 400
    same = await client.get("/api/users/page", params={**params, "cursor": first.next_cursor})
    assert same.status_code == 200
