"""Company list stats, keyset paging and pickers against a real PostgreSQL schema."""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from anyio import Path
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import admin
from app.core.security import create_access_token
from app.dao.agent_dao import agent_dao
from app.dao.identity_dao import identity_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db import connection as connection_module
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection

_BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


@dataclass
class CompanyWorld:
    db: DbConnection
    client: AsyncClient
    headers: dict[str, str]
    tenants: list[UUID]


async def _tenant(created_at: datetime, name: str, tenant_id: UUID | None = None) -> UUID:
    tenant = await tenant_dao.create(
        obj_in={"id": tenant_id or uuid4(), "name": name, "slug": f"s-{uuid4().hex[:12]}", "created_at": created_at}
    )
    return tenant.id


async def _user(
    tenant_id: UUID, *, role: str = "member", active: bool = True, email: str | None = None, at: datetime = _BASE
) -> UUID:
    identity_id = None
    if email is not None:
        identity_id = (await identity_dao.create(obj_in={"email": email, "email_verified": True})).id
    user = await user_dao.create(
        obj_in={
            "identity_id": identity_id,
            "tenant_id": tenant_id,
            "display_name": email or "no identity",
            "role": role,
            "is_active": active,
            "created_at": at,
        }
    )
    return user.id


async def _agent(tenant_id: UUID, creator_id: UUID, *, status: str, tokens: int, cache: int) -> None:
    _ = await agent_dao.create(
        obj_in={
            "id": uuid4(),
            "name": "agent",
            "creator_id": creator_id,
            "tenant_id": tenant_id,
            "status": status,
            "tokens_used_total": tokens,
            "cache_read_tokens_total": cache,
        }
    )


async def _seed_uneven(tag: str) -> list[UUID]:
    """Six tenants, newest first by index; uneven users and agents so a join-and-count would inflate."""
    empty = await _tenant(_BASE + timedelta(days=5), f"{tag}-empty")
    users_only = await _tenant(_BASE + timedelta(days=4), f"{tag}-users")
    for number in range(3):
        _ = await _user(users_only, active=number != 0)
    agents_only = await _tenant(_BASE + timedelta(days=3), f"{tag}-agents")
    owner = await _user(await _tenant(_BASE, f"{tag}-owner-home"))
    for number in range(2):
        await _agent(agents_only, owner, status="running" if number else "stopped", tokens=10 + number, cache=number)
    both = await _tenant(_BASE + timedelta(days=2), f"{tag}-both")
    creator = await _user(both, email=f"member-{tag}@x.test", at=_BASE)
    for number in range(4):
        _ = await _user(both, active=number % 2 == 0, at=_BASE + timedelta(hours=number))
    for number in range(3):
        await _agent(both, creator, status="running" if number == 0 else "idle", tokens=100 * (number + 1), cache=7)
    admins = await _tenant(_BASE + timedelta(days=1), f"{tag}-admins")
    _ = await _user(admins, role="org_admin", at=_BASE)  # earliest, but no identity
    _ = await _user(admins, role="org_admin", email=f"late-{tag}@x.test", at=_BASE + timedelta(hours=3))
    _ = await _user(admins, role="org_admin", email=f"early-{tag}@x.test", at=_BASE + timedelta(hours=1))
    _ = await _user(admins, role="member", email=f"member2-{tag}@x.test", at=_BASE)
    return [empty, users_only, agents_only, both, admins]


async def _oracle(tenant_id: UUID) -> dict[str, Any]:
    """Today's per-tenant DAO answers."""
    users = await user_dao.count_for_tenant(tenant_id)
    active = await user_dao.count_for_tenant(tenant_id, is_active=True)
    total_tokens, cache = await agent_dao.sum_tokens_for_tenant(tenant_id)
    from app.core.domain.agents import AgentStatus

    return {
        "user_count": users,
        "active_user_count": active,
        "inactive_user_count": users - active,
        "agent_count": await agent_dao.count_for_tenant(tenant_id),
        "agent_running_count": await agent_dao.count_for_tenant(tenant_id, status=AgentStatus.RUNNING),
        "total_tokens": total_tokens,
        "cache_read_tokens_total": cache,
        "org_admin_email": await user_dao.first_org_admin_email(tenant_id),
    }


@pytest.fixture
def conninfo() -> str:
    value = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL required for isolated PostgreSQL company QA")
    return value


@pytest.fixture
async def world(conninfo: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CompanyWorld]:
    schema = f"company_stats_{uuid4().hex}"
    monkeypatch.setenv("USER_SESSION_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("TENANT_CACHE_TTL_SECONDS", "0")
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(await Path("scripts/schema_baseline.sql").read_text())
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=4)
            home = await _tenant(_BASE - timedelta(days=30), "Home")
            admin_user = await _user(home, role="platform_admin", email="pa@x.test")
            token = await create_access_token(str(admin_user), "platform_admin")
            app = FastAPI()
            app.include_router(admin.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://companies.test") as client:
                yield CompanyWorld(db, client, {"Authorization": f"Bearer {token}"}, [home])
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    for method in ("fetchall", "fetchone", "fetchval", "execute"):
        original = getattr(DbConnection, method)

        def counted(self: DbConnection, query: str, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
            seen.append(query)
            return _original(self, query, *args, **kwargs)

        monkeypatch.setattr(connection_module.DbConnection, method, counted)
    return seen


async def _get(world: CompanyWorld, path: str, **params: Any) -> Any:
    return await world.client.get(f"/api/admin{path}", params=params, headers=world.headers)


async def test_stats_match_per_tenant_dao_answers_on_every_route(world: CompanyWorld):
    # Given tenants with no rows, users only, agents only, both, and org admins with and without identity.
    seeded = await _seed_uneven("p")
    expected = {tenant_id: await _oracle(tenant_id) for tenant_id in seeded}

    # When each route that returns CompanyStats answers.
    legacy = await _get(world, "/companies")
    page = await _get(world, "/companies/page", limit=200)

    # Then every row carries the numbers the per-tenant DAO calls return.
    assert isinstance(legacy.json(), list)
    assert page.json()["next_cursor"] is None
    for rows in (legacy.json(), page.json()["items"]):
        by_id = {UUID(row["id"]): row for row in rows}
        for tenant_id, want in expected.items():
            assert {key: by_id[tenant_id][key] for key in want} == want
    for tenant_id, want in expected.items():
        single = await _get(world, f"/companies/{tenant_id}/stats")
        assert {key: single.json()[key] for key in want} == want
    assert expected[seeded[0]]["user_count"] == 0
    assert expected[seeded[4]]["org_admin_email"] == "early-p@x.test"
    assert expected[seeded[3]]["agent_count"] == 3
    assert expected[seeded[3]]["user_count"] == 5


async def test_query_count_does_not_grow_with_page_size(world: CompanyWorld, statements: list[str]):
    # Given enough tenants for a small and a large page.
    for number in range(12):
        _ = await _tenant(_BASE + timedelta(minutes=number), f"bulk-{number}")

    # When a 2-row page and a 13-row page are read.
    counts: list[int] = []
    for limit in (2, 13):
        statements.clear()
        response = await _get(world, "/companies/page", limit=limit)
        assert len(response.json()["items"]) == limit
        counts.append(len([sql for sql in statements if "FROM tenants" in sql or "unnest" in sql]))

    # Then the statements over tenants, users and agents stay the same.
    assert counts[0] == counts[1] == 2


async def test_keyset_walk_never_repeats_or_skips_equal_created_at(world: CompanyWorld):
    # Given eight tenants that share two created_at values.
    fixed_ids = [UUID(int=number) for number in (4, 1, 8, 2, 7, 3, 6, 5)]
    for index, tenant_id in enumerate(fixed_ids):
        _ = await _tenant(_BASE + timedelta(days=2 if index < 4 else 1), f"tie-{index}", tenant_id)
    everything = [UUID(row["id"]) for row in (await _get(world, "/companies")).json()]

    # When the list is walked three at a time.
    walked: list[UUID] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        body = (await _get(world, "/companies/page", **params)).json()
        walked.extend(UUID(row["id"]) for row in body["items"])
        cursor = str(body["next_cursor"]) if body["next_cursor"] is not None else None
        if cursor is None:
            break

    # Then every tenant appears once, in created_at DESC, id DESC order.
    assert walked == everything
    assert len(set(walked)) == len(walked) == 9
    assert walked[:4] == [UUID(int=number) for number in (8, 4, 2, 1)]


async def test_exact_full_page_has_no_cursor_and_search_is_ranked_without_one(world: CompanyWorld):
    # Given exactly two more tenants than the seeded home tenant.
    _ = await _tenant(_BASE, "Mara Alpha")
    _ = await _tenant(_BASE + timedelta(days=1), "Mara Beta")

    # When a page asks for exactly all three, and when searching.
    full = (await _get(world, "/companies/page", limit=3)).json()
    ranked = (await _get(world, "/companies/page", q="mara", limit=1)).json()
    cjk = (await _get(world, "/companies/page", q="北京公司")).json()
    legacy_cjk = (await _get(world, "/companies", q="北京公司")).json()

    # Then there is no lookahead cursor, search ignores the page size, and tokenless input matches nothing.
    assert full["next_cursor"] is None
    assert [row["name"] for row in ranked["items"]] == ["Mara Beta", "Mara Alpha"]
    assert ranked["next_cursor"] is None
    assert cjk == {"items": [], "next_cursor": None}
    assert legacy_cjk == []


async def test_options_list_every_company_newest_first_without_stats(world: CompanyWorld):
    # Given more tenants than the largest page.
    for number in range(205):
        _ = await _tenant(_BASE + timedelta(minutes=number), f"opt-{number}")

    # When the picker list is read.
    response = await _get(world, "/companies/options")

    # Then it is complete, ordered like the legacy list, and carries no stats.
    options = response.json()
    assert len(options) == 206
    assert [row["id"] for row in options] == [row["id"] for row in (await _get(world, "/companies")).json()]
    assert "user_count" not in options[0]
    assert options[0]["can_disable"] is True


async def test_single_company_stats_404_and_zero_rows(world: CompanyWorld):
    missing = await _get(world, f"/companies/{uuid4()}/stats")
    assert missing.status_code == 404
    empty = (await _get(world, f"/companies/{world.tenants[0]}/stats")).json()
    assert (empty["user_count"], empty["agent_count"], empty["total_tokens"]) == (1, 0, 0)
