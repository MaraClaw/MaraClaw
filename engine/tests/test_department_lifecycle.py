from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib import import_module
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.core import acl_locks
from app.dao import user_dao
from app.dao.base import BaseDAO
from app.db import session
from app.records.user import UserRecord


class DirectoryConnection:
    def __init__(self, user: UserRecord):
        self.user = user
        self.assignment: UUID | None = uuid4()
        self.tenant_agents = {user.tenant_id: uuid4()}
        self.operations: list[str] = []
        self.tenant_locks: list[UUID] = []

    async def fetchone(self, query, params):
        if "FROM tenants" in query:
            self.tenant_locks.append(params["id"])
            return {"id": params["id"]}
        return {"tenant_id": self.user.tenant_id}

    async def fetchall(self, _query, params):
        return [{"id": self.tenant_agents[params["tenant"]]}]

    async def execute(self, query, _params):
        if "DELETE FROM local_department_memberships" in query:
            self.assignment = None
            self.operations.append("clear_local_assignment")


@pytest.mark.asyncio
async def test_transfer_clears_local_assignment_before_tenant_change_and_invalidates_both(monkeypatch):
    origin = uuid4()
    user = UserRecord(id=uuid4(), tenant_id=origin)
    target = uuid4()
    db = DirectoryConnection(user)
    db.tenant_agents[target] = uuid4()

    @asynccontextmanager
    async def context() -> AsyncIterator[DirectoryConnection]:
        yield db

    async def update(_self, *, db_obj, obj_in):
        assert db.assignment is None
        db.operations.append("change_tenant")
        return replace(db_obj, **obj_in)

    monkeypatch.setattr(session, "optional_connection_ctx", context)
    monkeypatch.setattr(BaseDAO, "update", update)
    module = import_module("app.dao.user_dao")
    session_bump = AsyncMock()
    agent_bump = AsyncMock()
    monkeypatch.setattr(module, "bump_user_session", session_bump)
    monkeypatch.setattr(acl_locks, "bump_agent_acl_version", agent_bump)
    result = await user_dao.update(db_obj=user, obj_in={"tenant_id": target})
    assert result.tenant_id == target
    assert db.operations == ["clear_local_assignment", "change_tenant"]
    assert db.tenant_locks == sorted([origin, target])
    assert {call.args[0] for call in agent_bump.await_args_list} == set(db.tenant_agents.values())
    assert session_bump.await_args is not None
    assert session_bump.await_args.args == (user.id,)


@pytest.mark.asyncio
async def test_moved_user_precondition_rejects_update_before_assignment_delete(monkeypatch):
    before = UserRecord(id=uuid4(), tenant_id=uuid4())
    current = replace(before, tenant_id=uuid4())
    db = DirectoryConnection(current)

    @asynccontextmanager
    async def context() -> AsyncIterator[DirectoryConnection]:
        yield db

    monkeypatch.setattr(session, "optional_connection_ctx", context)
    with pytest.raises(HTTPException) as error:
        await user_dao.update(db_obj=before, obj_in={"tenant_id": uuid4()})
    assert error.value.status_code == 409
    assert db.assignment is not None
    assert db.operations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["org_admin", "platform_admin"])
async def test_promotion_clears_department_and_invalidates_tenant_agents(monkeypatch, role):
    user = UserRecord(id=uuid4(), tenant_id=uuid4())
    db = DirectoryConnection(user)

    @asynccontextmanager
    async def context():
        yield db

    async def update(_self, *, db_obj, obj_in):
        assert db.assignment is None
        return replace(db_obj, **obj_in)

    monkeypatch.setattr(session, "optional_connection_ctx", context)
    monkeypatch.setattr(BaseDAO, "update", update)
    monkeypatch.setattr(import_module("app.dao.user_dao"), "bump_user_session", AsyncMock())
    bump = AsyncMock()
    monkeypatch.setattr(acl_locks, "bump_agent_acl_version", bump)
    result = await user_dao.update(db_obj=user, obj_in={"role": role})
    assert result.role == role
    assert db.assignment is None
    assert bump.await_args is not None
    assert bump.await_args.args == (db.tenant_agents[user.tenant_id],)


@pytest.mark.asyncio
async def test_nonpolicy_update_preserves_session_bump_without_acl_mutex(monkeypatch):
    user = UserRecord(id=uuid4(), tenant_id=uuid4())
    updated = replace(user, display_name="Updated")
    monkeypatch.setattr(BaseDAO, "update", AsyncMock(return_value=updated))
    mutex = AsyncMock(side_effect=AssertionError("Non-policy update acquired ACL mutex"))
    monkeypatch.setattr(acl_locks, "lock_tenants", mutex)
    bump = AsyncMock()
    monkeypatch.setattr(import_module("app.dao.user_dao"), "bump_user_session", bump)
    assert await user_dao.update(db_obj=user, obj_in={"display_name": "Updated"}) == updated
    bump.assert_awaited_once_with(user.id)
    mutex.assert_not_awaited()
