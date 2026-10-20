from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.errors import ForeignKeyViolation

from app.dao.org_department_dao import OrgDepartmentDAO
from app.db import session
from app.db.connection import DbConnection
from app.db.errors import ForeignKeyViolationError
from app.schemas.department import AssignmentsOut
from app.services import org_departments
from test_department_api import directory as directory


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", [False, True])
async def test_membership_dao_replaces_normalized_rows_in_one_transaction(clear):
    raw = AsyncMock(spec=AsyncConnection)
    cursor = raw.cursor.return_value.__aenter__.return_value
    db = DbConnection(raw)
    selected = [uuid4(), uuid4()]
    assignment = AssignmentsOut(
        user_id=uuid4(), tenant_id=uuid4(), department_ids=[] if clear else [*selected, selected[0]]
    )

    async with session.transaction(db):
        await OrgDepartmentDAO().replace_assignments(assignment)
        raw.commit.assert_not_awaited()

    calls = cursor.execute.await_args_list
    assert calls[0].args == (
        "DELETE FROM local_department_memberships WHERE user_id = %(user)s AND tenant_id = %(tenant)s",
        {"user": assignment.user_id, "tenant": assignment.tenant_id},
    )
    assert len(calls) == (1 if clear else 2)
    if not clear:
        assert "unnest(%(departments)s::uuid[])" in calls[1].args[0]
        assert calls[1].args[1] == {
            "user": assignment.user_id,
            "tenant": assignment.tenant_id,
            "departments": sorted(selected),
        }
    raw.commit.assert_awaited_once()
    raw.rollback.assert_not_awaited()
    assert session.get_connection() is None


@pytest.mark.asyncio
async def test_membership_insert_failure_rolls_back_delete():
    raw = AsyncMock(spec=AsyncConnection)
    cursor = raw.cursor.return_value.__aenter__.return_value
    cursor.execute.side_effect = [None, ForeignKeyViolation()]
    db = DbConnection(raw)
    assignment = AssignmentsOut(user_id=uuid4(), tenant_id=uuid4(), department_ids=[uuid4()])

    with pytest.raises(ForeignKeyViolationError):
        async with session.transaction(db):
            await OrgDepartmentDAO().replace_assignments(assignment)

    assert cursor.execute.await_count == 2
    raw.commit.assert_not_awaited()
    raw.rollback.assert_awaited_once()
    assert session.get_connection() is None


@pytest.mark.parametrize("failure", [False, True])
def test_membership_and_sorted_union_acl_updates_share_transaction(directory, monkeypatch, failure):
    client, _, subject, _, second, *_ = directory
    raw = AsyncMock(spec=AsyncConnection)
    db = DbConnection(raw)
    old_agent, shared_agent, new_agent = uuid4(), uuid4(), uuid4()
    locked, bumped = [], []

    @asynccontextmanager
    async def transaction():
        async with session.transaction(db):
            yield db

    async def lock_agent(connection, agent_id, _tenant_id):
        assert connection is db
        assert session.get_connection() is db
        locked.append(agent_id)

    async def bump_agent(agent_id):
        assert session.get_connection() is db
        raw.commit.assert_not_awaited()
        bumped.append(agent_id)
        if failure:
            raise RuntimeError("ACL update failed")

    monkeypatch.setattr(org_departments, "connection_ctx", transaction)
    monkeypatch.setattr(org_departments, "lock_agent", lock_agent)
    monkeypatch.setattr(org_departments, "bump_agent_acl_version", bump_agent)
    monkeypatch.setattr(
        org_departments.org_department_dao, "replace_assignments", OrgDepartmentDAO().replace_assignments
    )
    monkeypatch.setattr(
        org_departments.org_department_dao,
        "grant_agents_for_user",
        AsyncMock(side_effect=[{old_agent, shared_agent}, {shared_agent, new_agent}]),
    )

    if failure:
        with pytest.raises(RuntimeError, match="ACL update failed"):
            client.put(f"/api/users/{subject.id}/departments", json={"department_ids": [str(second.id)]})
        raw.commit.assert_not_awaited()
        raw.rollback.assert_awaited_once()
    else:
        response = client.put(f"/api/users/{subject.id}/departments", json={"department_ids": [str(second.id)]})
        assert response.status_code == 200
        assert locked == bumped == sorted([old_agent, shared_agent, new_agent])
        raw.commit.assert_awaited_once()
        raw.rollback.assert_not_awaited()
