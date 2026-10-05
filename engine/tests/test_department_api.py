from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import departments
from app.core.security import get_current_user
from app.db.errors import UniqueViolationError
from app.records.local_department import LocalDepartmentRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.department import AssignmentOut
from app.services import local_departments


@asynccontextmanager
async def transaction() -> AsyncIterator[None]:
    yield None


@pytest.fixture
def directory(monkeypatch):
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme")
    actor = UserRecord(id=uuid4(), tenant_id=tenant.id, role="org_admin")
    subject = UserRecord(id=uuid4(), tenant_id=tenant.id)
    first = LocalDepartmentRecord(id=uuid4(), tenant_id=tenant.id, name="Sales", created_at=datetime.now(UTC))
    second = LocalDepartmentRecord(id=uuid4(), tenant_id=tenant.id, name="Support", created_at=datetime.now(UTC))
    people = {actor.id: actor, subject.id: subject}
    department_rows = {first.id: first, second.id: second}
    assignments = {subject.id: first.id}
    old_agent, new_agent = uuid4(), uuid4()

    async def get_user(user_id, *, fresh=False):
        return people.get(user_id)

    async def get_department(department_id):
        return department_rows.get(department_id)

    async def assigned_agents(user_id):
        selected = assignments.get(user_id)
        return {old_agent} if selected == first.id else {new_agent} if selected == second.id else set()

    async def assign(assignment):
        if assignment.department_id is None:
            assignments.pop(assignment.user_id, None)
        else:
            assignments[assignment.user_id] = assignment.department_id

    async def list_assignments(_tenant):
        return [AssignmentOut(user_id=uid, tenant_id=tenant.id, department_id=did) for uid, did in assignments.items()]

    async def create(*, obj_in):
        if any(row.name.lower() == obj_in["name"].lower() for row in department_rows.values()):
            raise UniqueViolationError()
        row = LocalDepartmentRecord(id=uuid4(), tenant_id=tenant.id, name=obj_in["name"], created_at=datetime.now(UTC))
        department_rows[row.id] = row
        return row

    monkeypatch.setattr(departments, "connection_ctx", transaction)
    monkeypatch.setattr(local_departments, "connection_ctx", transaction)
    for module in (departments, local_departments):
        monkeypatch.setattr(module, "lock_tenants", AsyncMock())
        monkeypatch.setattr(module, "lock_user", AsyncMock())
    monkeypatch.setattr(local_departments, "lock_agent", AsyncMock())
    monkeypatch.setattr(local_departments.user_dao, "get", get_user)
    monkeypatch.setattr(local_departments.user_dao, "get_with_identity", get_user)
    monkeypatch.setattr(departments.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(local_departments.local_department_dao, "get", get_department)
    monkeypatch.setattr(local_departments.local_department_dao, "assign", assign)
    monkeypatch.setattr(local_departments.local_department_dao, "grant_agents_for_user", assigned_agents)
    monkeypatch.setattr(departments.local_department_dao, "list_assignments", list_assignments)
    monkeypatch.setattr(
        departments.local_department_dao, "list_for_tenant", AsyncMock(return_value=list(department_rows.values()))
    )
    monkeypatch.setattr(departments.local_department_dao, "create", create)
    bumped = AsyncMock()
    monkeypatch.setattr(local_departments, "bump_agent_acl_version", bumped)
    app = FastAPI()
    app.include_router(departments.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app), actor, subject, first, second, assignments, bumped, old_agent, new_agent, department_rows


def test_directory_and_creation_http_shapes(directory):
    client, actor, subject, first, *_ = directory
    response = client.get(f"/api/departments?tenant_id={actor.tenant_id}")
    assert response.status_code == 200
    assert response.json()["assignments"] == [
        {"user_id": str(subject.id), "tenant_id": str(actor.tenant_id), "department_id": str(first.id)}
    ]
    created = client.post(f"/api/departments?tenant_id={actor.tenant_id}", json={"name": "  Finance  "})
    assert created.status_code == 201
    assert created.json()["name"] == "Finance"
    assert set(created.json()) == {"id", "tenant_id", "name", "created_at"}


@pytest.mark.parametrize("operation", ["create", "assign"])
def test_privileged_write_rejects_fresh_password_change_requirement(directory, monkeypatch, operation):
    client, actor, subject, first, second, assignments, bumped, *rest = directory
    fresh_actor = UserRecord(id=actor.id, tenant_id=actor.tenant_id, role=actor.role)
    from app.records.identity import IdentityRecord

    fresh_actor.identity = IdentityRecord(id=uuid4(), must_change_password=True)
    monkeypatch.setattr(departments.user_dao, "get_with_identity", AsyncMock(return_value=fresh_actor))
    rows = rest[-1]
    count = len(rows)
    if operation == "create":
        response = client.post(f"/api/departments?tenant_id={actor.tenant_id}", json={"name": "Denied"})
    else:
        response = client.put(
            f"/api/users/{subject.id}/department?tenant_id={actor.tenant_id}", json={"department_id": str(second.id)}
        )
    assert response.status_code == 403
    assert assignments[subject.id] == first.id
    assert len(rows) == count
    bumped.assert_not_awaited()


@pytest.mark.parametrize("selection", ["move", "remove"])
def test_assignment_invalidates_old_and_new_department_grants(directory, selection):
    client, actor, subject, _, second, assignments, bumped, old_agent, new_agent, _ = directory
    selected = str(second.id) if selection == "move" else None
    response = client.put(
        f"/api/users/{subject.id}/department?tenant_id={actor.tenant_id}", json={"department_id": selected}
    )
    assert response.status_code == 200
    assert response.json() == {"user_id": str(subject.id), "tenant_id": str(actor.tenant_id), "department_id": selected}
    assert assignments.get(subject.id) == (second.id if selected else None)
    assert {call.args[0] for call in bumped.await_args_list} == ({old_agent, new_agent} if selected else {old_agent})


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("member_actor", 403),
        ("foreign_user", 403),
        ("missing_user", 404),
        ("admin_subject", 422),
        ("foreign_department", 422),
        ("missing_department", 422),
    ],
)
def test_invalid_assignment_does_not_mutate_directory(directory, case, status):
    client, actor, subject, first, second, assignments, bumped, *_ = directory
    subject_id = subject.id
    selected = second.id
    if case == "member_actor":
        actor.role = "member"
    elif case == "foreign_user":
        subject.tenant_id = uuid4()
    elif case == "missing_user":
        subject_id = uuid4()
    elif case == "admin_subject":
        subject.role = "org_admin"
    elif case == "foreign_department":
        directory[-1][second.id] = replace(second, tenant_id=uuid4())
    else:
        selected = uuid4()
    response = client.put(
        f"/api/users/{subject_id}/department?tenant_id={actor.tenant_id}", json={"department_id": str(selected)}
    )
    assert response.status_code == status
    assert assignments == {subject.id: first.id}
    assert not bumped.await_args_list


def test_inactive_subject_can_retain_assignment(directory):
    client, actor, subject, _, second, *_ = directory
    subject.is_active = False
    response = client.put(
        f"/api/users/{subject.id}/department?tenant_id={actor.tenant_id}", json={"department_id": str(second.id)}
    )
    assert response.status_code == 200


def test_duplicate_case_insensitive_department_maps_to_conflict(directory):
    client, actor, *_ = directory
    response = client.post(f"/api/departments?tenant_id={actor.tenant_id}", json={"name": "  sALES  "})
    assert response.status_code == 409


@pytest.mark.parametrize("name", ["   ", "a" * 101])
def test_invalid_department_name_is_rejected_at_http_boundary(directory, name):
    client, actor, *_ = directory
    assert client.post(f"/api/departments?tenant_id={actor.tenant_id}", json={"name": name}).status_code == 422
