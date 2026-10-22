from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.errors import ForeignKeyViolation

from app.api import departments
from app.dao.org_department_dao import OrgDepartmentDAO
from app.db import session
from app.db.connection import DbConnection, Row
from app.db.errors import ForeignKeyViolationError
from app.schemas.department import AssignmentsOut, DepartmentCreate
from app.services import org_departments
from department_delete_postgres_fixtures import (
    DepartmentStore,
    department_store as department_store,
    schema_db as schema_db,
)
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


async def _mutate_department(store: DepartmentStore, operation: str) -> None:
    match operation:
        case "create":
            await departments.create_department(DepartmentCreate(name="Created"), store.tenant, store.actor)
        case "rename":
            await org_departments.rename_department(
                store.actor, store.selected, DepartmentCreate(name="Renamed"), store.tenant
            )
        case "delete":
            await org_departments.delete_department(store.actor, store.selected, store.tenant)
        case "assign":
            await org_departments.assign_departments(
                store.actor,
                AssignmentsOut(user_id=store.members[0], tenant_id=store.tenant, department_ids=[store.other]),
            )
        case _:
            raise AssertionError(operation)


async def _department_state(db: DbConnection, store: DepartmentStore) -> tuple[list[Row], list[Row]]:
    return (
        await db.fetchall(
            "SELECT id, name FROM local_departments WHERE tenant_id = %(tenant)s ORDER BY id",
            {"tenant": store.tenant},
        ),
        await db.fetchall(
            "SELECT user_id, department_id FROM local_department_memberships "
            "WHERE tenant_id = %(tenant)s ORDER BY user_id, department_id",
            {"tenant": store.tenant},
        ),
    )


@pytest.mark.parametrize("operation", ["create", "rename", "delete", "assign"])
async def test_department_audit_rolls_back_with_genuine_outer_transaction(department_store, operation):
    # Given persisted departments and a real PostgreSQL connection, not an audit logging mock.
    store = department_store
    before = await _department_state(store.db, store)

    # When a caller rolls back after the owner has written both its mutation and audit.
    async def abort_owner_scope() -> None:
        async with session.connection_ctx() as db:
            await _mutate_department(store, operation)
            assert await _department_state(db, store) != before
            events = await db.fetchall(
                "SELECT actor_id, tenant_id, action FROM admin_audit_logs WHERE tenant_id = %(tenant)s",
                {"tenant": store.tenant},
            )
            assert events == [
                {
                    "actor_id": store.actor.id,
                    "tenant_id": store.tenant,
                    "action": "department.membership.replace" if operation == "assign" else f"department.{operation}",
                }
            ]
            assert store.invalidated == []
            raise RuntimeError("caller aborted")

    with pytest.raises(RuntimeError, match="caller aborted"):
        await abort_owner_scope()
    # Then the database restores membership/directory rows and removes the event; deferred ACLs never escape.
    assert await _department_state(store.db, store) == before
    assert await store.db.fetchall("SELECT id FROM admin_audit_logs") == []
    assert store.invalidated == []


@pytest.mark.parametrize("operation", ["create", "rename", "delete", "assign"])
async def test_department_audit_sql_failure_is_savepoint_isolated(department_store, operation):
    # Given a real database constraint that rejects the audit insert, after the owner mutation.
    store = department_store
    before = await _department_state(store.db, store)
    await store.db.execute(
        "ALTER TABLE admin_audit_logs ADD CONSTRAINT reject_department_audit CHECK (action NOT LIKE 'department.%')"
    )
    # When the real writer encounters an SQL error inside the owner's outer transaction.
    async with session.connection_ctx() as db:
        await _mutate_department(store, operation)
        assert await db.fetchval("SELECT 1") == 1
        assert await db.fetchall("SELECT id FROM admin_audit_logs") == []
        assert store.invalidated == []
    # Then the mutation commits, the connection remains usable, and only the failed audit is absent.
    assert await _department_state(store.db, store) != before
    assert await store.db.fetchall("SELECT id FROM admin_audit_logs") == []
    assert set(store.invalidated) == (set(store.agents[:2]) if operation in {"delete", "assign"} else set())
