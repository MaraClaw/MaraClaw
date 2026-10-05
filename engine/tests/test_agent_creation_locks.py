from collections.abc import Mapping
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.core.json_types import uuid_from_row
from app.dao import agent_dao, agent_permission_dao
from app.db import session
from app.db.connection import DbConnection, Params, Row
from app.db.session import connection_ctx, transaction


class CreationConnection(DbConnection):
    def __init__(self, tenant: UUID | None, current: UUID | None):
        self.tenant = tenant
        self.current = current
        self.operations: list[tuple[str, object]] = []
        self.agent_tenant: UUID | None = None
        self.commits = 0
        self.rollbacks = 0

    async def fetchone(self, query: str, params: Params = None) -> Row | None:
        assert isinstance(params, Mapping)
        assert session.get_connection() is self
        if "FROM tenants" in query:
            self.operations.append(("tenant", params["id"]))
            return {"id": params["id"]}
        if "FROM users" in query:
            locked = "FOR UPDATE" in query
            self.operations.append(("creator" if locked else "observe", params["id"]))
            return {"tenant_id": self.current if locked else self.tenant}
        if query.startswith("INSERT INTO agents "):
            self.operations.append(("insert", params["id"]))
            tenant = params.get("tenant_id")
            self.agent_tenant = uuid_from_row(tenant) if tenant is not None else None
            return dict(params)
        if "FROM agents" in query:
            return {"tenant_id": self.agent_tenant}
        if query.startswith("INSERT INTO agent_permissions "):
            self.operations.append(("permission", params["agent_id"]))
            return dict(params)
        raise AssertionError(query)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("creator_tenant", [UUID(int=1), UUID(int=3), None])
async def test_creation_locks_actual_creator_and_target_before_insert_through_grants(monkeypatch, creator_tenant):
    # Given: a cross-tenant (or tenantless platform) creator, real DAO inserts.
    target, creator, agent_id = UUID(int=2), uuid4(), uuid4()
    db = CreationConnection(creator_tenant, creator_tenant)

    async def bump(_agent_id):
        return None

    monkeypatch.setattr(import_module("app.dao.agent_dao"), "bump_agent_acl_version", bump)

    # When: nested creation and grants share the caller's transaction.
    async with transaction(db):
        agent = await agent_dao.create(
            obj_in={"id": agent_id, "name": "fixture", "creator_id": creator, "tenant_id": target}
        )
        await agent_permission_dao.create(obj_in={"agent_id": agent.id, "scope_type": "company"})
        assert db.commits == 0
        async with connection_ctx() as nested:
            assert nested is db

    # Then: sorted mutexes precede creator/FK locks; no intermediate commit.
    tenants = sorted({item for item in (target, creator_tenant) if item is not None})
    assert db.operations == [
        ("observe", creator),
        *[("tenant", t) for t in tenants],
        ("creator", creator),
        ("insert", agent_id),
        ("tenant", target),
        ("permission", agent_id),
    ]
    assert db.commits == 1
    assert agent.tenant_id == target
    assert agent.creator_id == creator


@pytest.mark.asyncio
async def test_changed_creator_tenant_rejects_before_insert_without_locking_new_tenant():
    # Given: transfer won after the unlocked creator observation.
    old, target, moved, creator = UUID(int=1), UUID(int=2), UUID(int=3), uuid4()
    db = CreationConnection(old, moved)

    # When
    with pytest.raises(HTTPException) as error:
        async with transaction(db):
            await agent_dao.create(obj_in={"name": "fixture", "creator_id": creator, "tenant_id": target})

    # Then: no out-of-order retry or partially inserted agent.
    assert error.value.status_code == 409
    assert db.operations == [("observe", creator), ("tenant", old), ("tenant", target), ("creator", creator)]
    assert db.rollbacks == 1
