import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import agents
from app.core import access_cache, permissions, row_memo
from app.core.domain.agents import AgentAccessReason, AgentStatus, PermissionGrantScope
from app.core.domain.users import UserRole
from app.dao import agent_dao, agent_permission_dao
from app.db.connection import DbConnection, Params
from app.db.pool import close_pool, init_pool
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.scripts import bootstrap_db
from app.services import onboarding
from app.services.agent_lifecycle import LifecycleSuccess
from test_access_cache import FakeRedis


@dataclass(frozen=True, slots=True)
class AccessAPI:
    db: DbConnection
    client: AsyncClient
    viewer: UserRecord
    agent: AgentRecord
    department_ids: tuple[UUID, UUID]


@pytest.fixture
async def access_api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AccessAPI]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"explain_access_{uuid4().hex}"
    access_cache.clear_request_memo()
    row_memo.clear_row_memo()
    fake = FakeRedis()
    monkeypatch.setattr(access_cache, "get_redis", AsyncMock(return_value=fake))
    monkeypatch.setattr(access_cache, "_ttl_seconds", lambda: 45)
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=2)
            await bootstrap_db._apply_sql_script(bootstrap_db.SCHEMA_BASELINE)
            tenant_id, owner_id, agent_id = uuid4(), uuid4(), uuid4()
            viewer = UserRecord(uuid4(), tenant_id=tenant_id)
            await db.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Access QA', 'access-qa')", {"id": tenant_id}
            )
            for user_id in (owner_id, viewer.id):
                await db.execute(
                    "INSERT INTO users (id, tenant_id, display_name) VALUES (%(id)s, %(tenant)s, 'Access QA')",
                    {"id": user_id, "tenant": tenant_id},
                )
            agent = await agent_dao.create(
                obj_in={
                    "id": agent_id,
                    "name": "Shared",
                    "creator_id": owner_id,
                    "tenant_id": tenant_id,
                    "access_mode": "custom",
                }
            )
            departments = (uuid4(), uuid4())
            for index, department_id in enumerate(departments):
                await db.execute(
                    "INSERT INTO local_departments (id, tenant_id, name) VALUES (%(id)s, %(tenant)s, %(name)s)",
                    {"id": department_id, "tenant": tenant_id, "name": f"Private department {index}"},
                )
                await db.execute(
                    "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) "
                    "VALUES (%(user)s, %(tenant)s, %(department)s)",
                    {"user": viewer.id, "tenant": tenant_id, "department": department_id},
                )
            for scope, scope_id in (("user", viewer.id), *(("department", dep) for dep in departments)):
                await db.execute(
                    "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
                    "VALUES (%(id)s, %(agent)s, %(scope)s, %(subject)s, %(level)s)",
                    {
                        "id": uuid4(),
                        "agent": agent.id,
                        "scope": scope,
                        "subject": scope_id,
                        "level": "manage" if scope == "user" else "use",
                    },
                )
            monkeypatch.setattr(agents.user_dao, "get_with_identity", AsyncMock(return_value=None))
            app = FastAPI()
            app.include_router(agents.router, prefix="/api")
            app.dependency_overrides[agents.get_current_user] = lambda: viewer
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://access-qa") as client:
                yield AccessAPI(db, client, viewer, agent, departments)
        finally:
            access_cache.clear_request_memo()
            row_memo.clear_row_memo()
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("reason", ["creator", "administrator", "company", "direct", "department"])
async def test_list_and_detail_agree_on_winning_reason(access_api: AccessAPI, reason: str) -> None:
    # Given overlapping grants with one expected policy winner.
    api = access_api
    if reason == "creator":
        await api.db.execute(
            "UPDATE agents SET creator_id = %(user)s WHERE id = %(agent)s",
            {"user": api.viewer.id, "agent": api.agent.id},
        )
    elif reason == "administrator":
        api.viewer.role = UserRole.ORG_ADMIN
        await api.db.execute("UPDATE users SET role = 'org_admin' WHERE id = %(id)s", {"id": api.viewer.id})
    elif reason == "company":
        await api.db.execute("UPDATE agents SET access_mode = 'company' WHERE id = %(id)s", {"id": api.agent.id})
    elif reason == "department":
        await api.db.execute("DELETE FROM agent_permissions WHERE scope_type = 'user'")
    # When the viewer requests both directory and detail responses.
    listed = await api.client.get("/api/agents/")
    detail = await api.client.get(f"/api/agents/{api.agent.id}")
    # Then both serializers disclose only the same winning reason, not underlying grants.
    assert listed.status_code == detail.status_code == 200
    assert listed.json()[0]["access_reason"] == detail.json()["access_reason"] == reason
    assert detail.json()["access_level"] == ("use" if reason in ("company", "department") else "manage")
    assert not {"department_ids", "department_access", "user_access", "permissions"} & detail.json().keys()
    assert "Private department" not in listed.text + detail.text


@pytest.mark.parametrize("revoked", ["direct", "membership", "department_grant"])
async def test_revocation_removes_warm_cached_access_and_explanation(access_api: AccessAPI, revoked: str) -> None:
    # Given a positive cached decision from the last remaining kind of grant.
    api = access_api
    keep = "user" if revoked == "direct" else "department"
    await api.db.execute("DELETE FROM agent_permissions WHERE scope_type <> %(keep)s", {"keep": keep})
    warm = await api.client.get(f"/api/agents/{api.agent.id}")
    assert warm.status_code == 200
    assert warm.json()["access_reason"] == ("direct" if revoked == "direct" else "department")
    # When the last matching grant or memberships are revoked and the existing ACL version is bumped.
    statement = (
        "DELETE FROM local_department_memberships" if revoked == "membership" else "DELETE FROM agent_permissions"
    )
    await api.db.execute(statement)
    await access_cache.bump_agent_acl_version(api.agent.id)
    row_memo.clear_row_memo()
    listed = await api.client.get("/api/agents/")
    detail = await api.client.get(f"/api/agents/{api.agent.id}")
    # Then neither the directory nor detail leaks a retired explanation or retains access.
    assert listed.status_code == 200
    assert listed.json() == []
    assert detail.status_code == 403
    assert "access_reason" not in detail.json()


async def test_direct_revocation_falls_back_to_remaining_department(access_api: AccessAPI) -> None:
    # Given overlapping direct manage and multiple department use grants.
    api = access_api
    assert (await api.client.get(f"/api/agents/{api.agent.id}")).json()["access_reason"] == "direct"
    # When direct access and one of two department memberships disappear.
    await api.db.execute("DELETE FROM agent_permissions WHERE scope_type = 'user'")
    await api.db.execute(
        "DELETE FROM local_department_memberships WHERE department_id = %(id)s", {"id": api.department_ids[0]}
    )
    await access_cache.bump_agent_acl_version(api.agent.id)
    row_memo.clear_row_memo()
    detail = await api.client.get(f"/api/agents/{api.agent.id}")
    # Then the remaining department yields use access, not stale direct management.
    assert detail.status_code == 200
    assert (detail.json()["access_level"], detail.json()["access_reason"]) == ("use", "department")


async def test_replaced_viewer_does_not_inherit_previous_creator_reason(access_api: AccessAPI) -> None:
    # Given a creator request followed by another authenticated member in the same company.
    api = access_api
    member_id = api.viewer.id
    api.viewer.id = api.agent.creator_id
    assert (await api.client.get(f"/api/agents/{api.agent.id}")).json()["access_reason"] == "creator"
    # When the session principal changes, without changing the agent or ACL.
    api.viewer.id = member_id
    detail = await api.client.get(f"/api/agents/{api.agent.id}")
    # Then the second viewer receives only their own direct grant explanation.
    assert detail.status_code == 200
    assert detail.json()["access_reason"] == "direct"


async def test_foreign_tenant_creator_is_denied_without_explanation(access_api: AccessAPI) -> None:
    # Given an agent whose creator and direct grantee has moved to another tenant.
    api = access_api
    foreign_id = uuid4()
    await api.db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Foreign', 'foreign')", {"id": foreign_id}
    )
    await api.db.execute("DELETE FROM local_department_memberships")
    await api.db.execute(
        "UPDATE users SET tenant_id = %(tenant)s, role = 'org_admin' WHERE id = %(id)s",
        {"tenant": foreign_id, "id": api.viewer.id},
    )
    await api.db.execute(
        "UPDATE agents SET creator_id = %(user)s WHERE id = %(agent)s", {"user": api.viewer.id, "agent": api.agent.id}
    )
    api.viewer.tenant_id = foreign_id
    api.viewer.role = UserRole.ORG_ADMIN
    # When the foreign creator requests the list and detail.
    listed = await api.client.get("/api/agents/")
    detail = await api.client.get(f"/api/agents/{api.agent.id}")
    # Then tenant isolation wins before creator, administrator, and direct grants.
    assert listed.status_code == 200
    assert listed.json() == []
    assert detail.status_code == 403
    assert "access_reason" not in detail.json()


@pytest.mark.parametrize("operation", ["start", "stop"])
async def test_lifecycle_responses_preserve_direct_manager_reason(
    access_api: AccessAPI, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    api = access_api
    api.agent.status = AgentStatus.RUNNING if operation == "start" else AgentStatus.STOPPED
    monkeypatch.setattr(agents, f"{operation}_agent_guest", AsyncMock(return_value=LifecycleSuccess(api.agent)))
    monkeypatch.setattr(onboarding, "is_onboarded", AsyncMock(return_value=False))

    response = await api.client.post(f"/api/agents/{api.agent.id}/{operation}")

    assert response.status_code == 200
    assert response.json()["access_reason"] == "direct"
    assert response.json()["status"] == api.agent.status


async def test_update_explicit_dump_preserves_administrator_reason(access_api: AccessAPI) -> None:
    api = access_api
    api.viewer.role = UserRole.ORG_ADMIN
    await api.db.execute("UPDATE users SET role = 'org_admin' WHERE id = %(id)s", {"id": api.viewer.id})

    response = await api.client.patch(f"/api/agents/{api.agent.id}", json={"bio": "Updated"})

    assert response.status_code == 200
    assert response.json()["access_reason"] == "administrator"


async def _insert_agent(
    api: AccessAPI, mode: str, *, level: str = "use", tenant_id: UUID | None = None, creator_id: UUID | None = None
) -> AgentRecord:
    return await agent_dao.create(
        obj_in={
            "id": uuid4(),
            "name": f"{mode} agent",
            "creator_id": creator_id or api.agent.creator_id,
            "tenant_id": tenant_id or api.viewer.tenant_id,
            "access_mode": mode,
            "company_access_level": level,
        }
    )


async def _grant(api: AccessAPI, agent: AgentRecord, scope: str, subject: UUID | None, level: str = "use") -> None:
    await api.db.execute(
        "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
        "VALUES (%(id)s, %(agent)s, %(scope)s, %(subject)s, %(level)s)",
        {"id": uuid4(), "agent": agent.id, "scope": scope, "subject": subject, "level": level},
    )


async def _seed_every_outcome(api: AccessAPI) -> list[AgentRecord]:
    viewer = api.viewer
    stranger, foreign_tenant, empty_department = uuid4(), uuid4(), uuid4()
    await api.db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Other', 'other-access')", {"id": foreign_tenant}
    )
    await api.db.execute(
        "INSERT INTO local_departments (id, tenant_id, name) VALUES (%(id)s, %(tenant)s, 'Nobody')",
        {"id": empty_department, "tenant": viewer.tenant_id},
    )
    agents_ = [api.agent]
    agents_.append(await _insert_agent(api, "private", creator_id=viewer.id))
    agents_.append(await _insert_agent(api, "private"))
    agents_.append(await _insert_agent(api, "company", level="manage"))
    agents_.append(await _insert_agent(api, "company", level="use"))
    default = await _insert_agent(api, "company", level="")
    agents_.append(default)
    legacy = await _insert_agent(api, "company", level="")
    await _grant(api, legacy, "company", None, "manage")
    agents_.append(legacy)
    for level in ("manage", "use"):
        direct = await _insert_agent(api, "custom")
        await _grant(api, direct, "user", viewer.id, level)
        await _grant(api, direct, "department", api.department_ids[0])
        agents_.append(direct)
    via_department = await _insert_agent(api, "custom")
    await _grant(api, via_department, "user", stranger, "manage")
    await _grant(api, via_department, "department", api.department_ids[1])
    agents_.append(via_department)
    denied = await _insert_agent(api, "custom")
    await _grant(api, denied, "user", stranger, "manage")
    await _grant(api, denied, "department", empty_department)
    agents_.extend([denied, await _insert_agent(api, "custom")])
    agents_.append(await _insert_agent(api, "custom", tenant_id=foreign_tenant, creator_id=viewer.id))
    return agents_


@pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.ORG_ADMIN])
@pytest.mark.parametrize("active", [True, False])
async def test_batch_decisions_match_single_agent_decisions(
    access_api: AccessAPI, role: UserRole, active: bool
) -> None:
    # Given every access outcome, with an optionally deactivated viewer.
    api = access_api
    catalog = await _seed_every_outcome(api)
    await api.db.execute(
        "UPDATE users SET is_active = %(active)s WHERE id = %(id)s", {"active": active, "id": api.viewer.id}
    )
    viewer = replace(api.viewer, role=role)
    # When resolving the catalog one agent at a time and in one batch.
    single = {agent.id: await permissions.compute_agent_access_decision(viewer, agent) for agent in catalog}
    batch = await permissions.compute_agent_access_decisions(viewer, catalog)
    # Then every level and reason matches, and the catalog exercised each reason or a denial.
    assert batch == single
    reasons = {decision.reason if decision else None for decision in batch.values()}
    if role == UserRole.MEMBER:
        assert reasons == {
            AgentAccessReason.CREATOR,
            AgentAccessReason.COMPANY,
            AgentAccessReason.DIRECT,
            None,
            *([AgentAccessReason.DEPARTMENT] if active else []),
        }
    else:
        assert reasons == {AgentAccessReason.CREATOR, AgentAccessReason.ADMINISTRATOR, None}


async def test_decision_rows_cover_only_company_and_viewer_rows(access_api: AccessAPI) -> None:
    # Given grants for the viewer, other users, departments and company sharing.
    api = access_api
    catalog = await _seed_every_outcome(api)
    # When fetching decision rows for the whole catalog.
    rows = await agent_permission_dao.list_decision_rows([agent.id for agent in catalog], api.viewer.id)
    # Then only company rows and the viewer's own user rows come back, grouped under their agent.
    flat = [row for group in rows.values() for row in group]
    assert {row.scope_type for row in flat} == {PermissionGrantScope.COMPANY, PermissionGrantScope.USER}
    assert all(row.scope_type == PermissionGrantScope.COMPANY or row.scope_id == api.viewer.id for row in flat)
    assert all(row.agent_id == agent_id for agent_id, group in rows.items() for row in group)


async def _list_with_query_count(
    api: AccessAPI, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[dict[str, object]]]:
    seen: list[str] = []
    with monkeypatch.context() as spying:
        for operation in ("execute", "fetchone", "fetchall", "fetchval"):
            original: Callable[..., Awaitable[object]] = getattr(DbConnection, operation)

            async def spy(
                self: DbConnection,
                query: str,
                params: Params = None,
                *,
                _original: Callable[..., Awaitable[object]] = original,
            ) -> object:
                seen.append(query)
                return await _original(self, query, params)

            spying.setattr(DbConnection, operation, spy)
        response = await api.client.get("/api/agents/?limit=500")
    assert response.status_code == 200
    return seen, cast("list[dict[str, object]]", response.json())


async def test_list_query_count_does_not_grow_with_the_catalog(
    access_api: AccessAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a viewer who reaches a few agents only through a department.
    api = access_api
    for _ in range(2):
        await _grant(api, await _insert_agent(api, "custom"), "department", api.department_ids[0])
    few, listed_few = await _list_with_query_count(api, monkeypatch)
    # When ten more department and direct agents join the catalog.
    for index in range(10):
        agent = await _insert_agent(api, "custom")
        await _grant(api, agent, "department", api.department_ids[index % 2])
        if index % 3 == 0:
            await _grant(api, agent, "user", api.viewer.id)
    many, listed_many = await _list_with_query_count(api, monkeypatch)
    # Then the list issues the same statements, none of them per agent.
    assert (len(listed_few), len(listed_many)) == (3, 13)
    assert len(many) == len(few)
    assert {row["access_reason"] for row in listed_many} == {"direct", "department"}
    assert sum("agent_id = ANY" in query and "FROM agent_permissions" in query for query in many) == 1
