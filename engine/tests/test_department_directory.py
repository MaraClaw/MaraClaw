from dataclasses import replace
from typing import Literal, assert_never
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.records.identity import IdentityRecord
from app.services import org_departments
from test_department_api import directory as directory


@pytest.mark.parametrize("role", ["org_admin", "member", "agent_admin", "platform_admin"])
def test_options_expose_only_own_tenant_ids_and_names(directory, role):
    client, actor, _, first, second, *rest = directory
    actor.role = role
    foreign = replace(first, id=uuid4(), tenant_id=uuid4(), name="Foreign")
    rest[-1][foreign.id] = foreign

    response = client.get(f"/api/departments/options?tenant_id={foreign.tenant_id}")

    assert response.status_code == 200
    assert response.json() == {
        "departments": [{"id": str(first.id), "name": first.name}, {"id": str(second.id), "name": second.name}]
    }


@pytest.mark.parametrize("tenantless", [False, True])
def test_options_return_empty_directory_without_own_departments(directory, tenantless):
    client, actor, *rest = directory
    if tenantless:
        actor.tenant_id = None
    else:
        rest[-1].clear()

    response = client.get("/api/departments/options")

    assert response.status_code == 200
    assert response.json() == {"departments": []}


@pytest.mark.parametrize("role", ["member", "agent_admin"])
def test_admin_directory_remains_denied_to_members(directory, role):
    client, actor, *_ = directory
    actor.role = role

    response = client.get("/api/departments")

    assert response.status_code == 403


def test_options_require_authentication(directory):
    client, *_ = directory
    client.app.dependency_overrides.clear()

    response = client.get("/api/departments/options")

    assert response.status_code == 401


@pytest.mark.parametrize(("name", "expected"), [("  Finance  ", "Finance"), ("sALES", "sALES"), ("a" * 100, "a" * 100)])
def test_rename_preserves_department_identity_and_assignments(directory, name, expected):
    client, actor, subject, first, _, assignments, bumped, *rest = directory

    response = client.patch(f"/api/departments/{first.id}", json={"name": name})

    assert response.status_code == 200
    assert response.json() == {
        "id": str(first.id),
        "tenant_id": str(actor.tenant_id),
        "name": expected,
        "created_at": first.created_at.isoformat().replace("+00:00", "Z"),
    }
    assert rest[-1][first.id] == replace(first, name=expected)
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()


def test_rename_duplicate_name_conflicts_without_mutation(directory):
    client, _, subject, first, second, assignments, bumped, *rest = directory

    response = client.patch(f"/api/departments/{first.id}", json={"name": f"  {second.name.upper()}  "})

    assert response.status_code == 409
    assert rest[-1][first.id] == first
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()


@pytest.mark.parametrize("foreign", [False, True])
def test_rename_missing_or_foreign_department_is_not_found(directory, foreign):
    client, _, _, first, *rest = directory
    if foreign:
        rest[-1][first.id] = replace(first, tenant_id=uuid4())
        department_id = first.id
    else:
        department_id = uuid4()
    before = dict(rest[-1])

    response = client.patch(f"/api/departments/{department_id}", json={"name": "Denied"})

    assert response.status_code == 404
    assert rest[-1] == before


@pytest.mark.parametrize("role", ["member", "agent_admin"])
def test_rename_requires_tenant_administrator(directory, role):
    client, actor, _, first, *rest = directory
    actor.role = role

    response = client.patch(f"/api/departments/{first.id}", json={"name": "Denied"})

    assert response.status_code == 403
    assert rest[-1][first.id] == first


def test_rename_rejects_org_admin_other_tenant_selector(directory):
    client, _, _, first, *rest = directory

    response = client.patch(f"/api/departments/{first.id}?tenant_id={uuid4()}", json={"name": "Denied"})

    assert response.status_code == 403
    assert rest[-1][first.id] == first


@pytest.mark.parametrize("selected", [False, True])
def test_platform_admin_rename_requires_explicit_tenant(directory, selected):
    client, actor, _, first, *_ = directory
    actor.role = "platform_admin"
    actor.tenant_id = uuid4()
    query = f"?tenant_id={first.tenant_id}" if selected else ""

    response = client.patch(f"/api/departments/{first.id}{query}", json={"name": "Finance"})

    assert response.status_code == (200 if selected else 422)


@pytest.mark.parametrize("case", ["missing", "inactive", "password", "demoted", "transferred"])
def test_rename_rechecks_fresh_actor_before_mutation(
    directory, monkeypatch, case: Literal["missing", "inactive", "password", "demoted", "transferred"]
):
    client, actor, subject, first, _, assignments, bumped, *rest = directory
    fresh_actor = replace(actor)
    match case:
        case "missing":
            fresh_actor = None
        case "inactive":
            fresh_actor.is_active = False
        case "password":
            fresh_actor.identity = IdentityRecord(id=uuid4(), must_change_password=True)
        case "demoted":
            fresh_actor.role = "member"
        case "transferred":
            fresh_actor.tenant_id = uuid4()
        case unexpected:
            assert_never(unexpected)
    loader = AsyncMock(return_value=fresh_actor)
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", loader)

    response = client.patch(f"/api/departments/{first.id}", json={"name": "Denied"})

    assert response.status_code == 403
    assert rest[-1][first.id] == first
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()
    loader.assert_awaited_once_with(actor.id, fresh=True)


def test_rename_locks_tenants_and_actor_before_fresh_authorization(directory, monkeypatch):
    client, actor, _, first, *_ = directory
    calls = AsyncMock()
    monkeypatch.setattr(org_departments, "lock_tenants", calls.lock_tenants)
    monkeypatch.setattr(org_departments, "lock_user", calls.lock_user)
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", calls.load_actor)
    calls.load_actor.return_value = actor

    response = client.patch(f"/api/departments/{first.id}?tenant_id={actor.tenant_id}", json={"name": "Finance"})

    assert response.status_code == 200
    assert [entry[0] for entry in calls.mock_calls] == ["lock_tenants", "lock_user", "load_actor"]
    calls.lock_tenants.assert_awaited_once_with(None, (actor.tenant_id, actor.tenant_id))
    calls.lock_user.assert_awaited_once_with(None, actor.id, actor.tenant_id)
    calls.load_actor.assert_awaited_once_with(actor.id, fresh=True)


@pytest.mark.parametrize("body", [{}, {"name": "   "}, {"name": "a" * 101}, {"name": None}, {"name": 1}])
def test_rename_validates_name_at_http_boundary(directory, body):
    client, _, _, first, *rest = directory

    response = client.patch(f"/api/departments/{first.id}", json=body)

    assert response.status_code == 422
    assert rest[-1][first.id] == first


@pytest.mark.parametrize("path", ["not-a-uuid", f"{uuid4()}?tenant_id=not-a-uuid"])
def test_rename_validates_resource_and_tenant_ids(directory, path):
    client, *_ = directory

    response = client.patch(f"/api/departments/{path}", json={"name": "Finance"})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "case", ["platform_role", "platform_identity", "inactive", "inactive_identity", "tenantless", "unknown", "password"]
)
def test_roster_requires_active_non_platform_tenant_caller(
    directory,
    case: Literal[
        "platform_role", "platform_identity", "inactive", "inactive_identity", "tenantless", "unknown", "password"
    ],
):
    client, actor, _, first, *_ = directory
    match case:
        case "platform_role":
            actor.role = "platform_admin"
        case "platform_identity":
            actor.identity = IdentityRecord(id=uuid4(), is_platform_admin=True)
        case "inactive":
            actor.is_active = False
        case "inactive_identity":
            actor.identity = IdentityRecord(id=uuid4(), is_active=False)
        case "tenantless":
            actor.tenant_id = None
        case "unknown":
            actor.role = "unknown"
        case "password":
            actor.identity = IdentityRecord(id=uuid4(), must_change_password=True)
        case unexpected:
            assert_never(unexpected)

    response = client.get(f"/api/departments/{first.id}/members")

    assert response.status_code == 403


@pytest.mark.parametrize("foreign", [False, True])
def test_roster_missing_and_foreign_departments_have_identical_not_found(directory, foreign):
    client, _, _, first, *rest = directory
    department_id = first.id if foreign else uuid4()
    rest[-1][first.id] = replace(first, tenant_id=uuid4())

    response = client.get(f"/api/departments/{department_id}/members")

    assert response.status_code == 404
    assert response.json() == {"detail": "Department not found"}


@pytest.mark.parametrize("authenticated", [False, True])
def test_roster_validates_authentication_and_department_id(directory, authenticated):
    client, *_ = directory
    if not authenticated:
        client.app.dependency_overrides.clear()

    response = client.get("/api/departments/not-a-uuid/members")

    assert response.status_code == (422 if authenticated else 401)
