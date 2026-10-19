from dataclasses import replace
from typing import Literal, assert_never
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.records.identity import IdentityRecord
from app.services import local_departments
from test_department_api import directory as directory


def test_plural_replacement_normalizes_duplicates_and_retains_row_directory(directory):
    client, actor, subject, first, second, assignments, bumped, old_agent, new_agent, _ = directory
    selected = sorted([first.id, second.id])

    response = client.put(
        f"/api/users/{subject.id}/departments", json={"department_ids": [str(second.id), str(first.id), str(second.id)]}
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(subject.id),
        "tenant_id": str(actor.tenant_id),
        "department_ids": list(map(str, selected)),
    }
    assert assignments == {subject.id: set(selected)}
    assert [call.args[0] for call in bumped.await_args_list] == sorted([old_agent, new_agent])
    listing = client.get("/api/departments").json()
    assert listing["assignments"] == [
        {"user_id": str(subject.id), "tenant_id": str(actor.tenant_id), "department_id": str(department_id)}
        for department_id in selected
    ]


@pytest.mark.parametrize("plural", [False, True])
@pytest.mark.parametrize("clear", [False, True])
def test_replacement_removes_every_old_membership(directory, plural, clear):
    client, _, subject, first, second, assignments, bumped, old_agent, new_agent, _ = directory
    assignments[subject.id] = {first.id, second.id}
    path = "departments" if plural else "department"
    body = (
        {"department_ids": [] if clear else [str(second.id)]}
        if plural
        else {"department_id": None if clear else str(second.id)}
    )

    response = client.put(f"/api/users/{subject.id}/{path}", json=body)

    assert response.status_code == 200
    assert assignments == ({} if clear else {subject.id: {second.id}})
    assert [call.args[0] for call in bumped.await_args_list] == sorted([old_agent, new_agent])


@pytest.mark.parametrize("foreign", [False, True])
def test_invalid_department_in_plural_set_preserves_every_old_membership(directory, foreign):
    client, _, subject, first, second, assignments, bumped, *rest = directory
    invalid_id = uuid4()
    if foreign:
        rest[-1][invalid_id] = replace(first, id=invalid_id, tenant_id=uuid4())

    response = client.put(
        f"/api/users/{subject.id}/departments", json={"department_ids": [str(second.id), str(invalid_id)]}
    )

    assert response.status_code == 422
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [{}, {"department_ids": None}, {"department_ids": "bad"}, {"department_ids": [None]}, {"department_ids": ["bad"]}],
)
def test_plural_request_requires_uuid_array(directory, body):
    client, _, subject, first, _, assignments, bumped, *_ = directory

    response = client.put(f"/api/users/{subject.id}/departments", json=body)

    assert response.status_code == 422
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()


@pytest.mark.parametrize("case", ["missing", "inactive", "password", "demoted", "transferred"])
def test_plural_rechecks_actor_under_tenant_and_user_locks(
    directory, monkeypatch, case: Literal["missing", "inactive", "password", "demoted", "transferred"]
):
    client, actor, subject, first, second, assignments, bumped, *_ = directory
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
    calls = AsyncMock()
    calls.load.return_value = fresh_actor
    monkeypatch.setattr(local_departments, "lock_tenants", calls.tenants)
    monkeypatch.setattr(local_departments, "lock_user", calls.user)
    monkeypatch.setattr(local_departments.user_dao, "get_with_identity", calls.load)

    response = client.put(f"/api/users/{subject.id}/departments", json={"department_ids": [str(second.id)]})

    assert response.status_code == 403
    assert assignments == {subject.id: {first.id}}
    assert [entry[0] for entry in calls.mock_calls] == ["tenants", "user", "load"]
    calls.load.assert_awaited_once_with(actor.id, fresh=True)
    bumped.assert_not_awaited()


@pytest.mark.parametrize(("case", "status"), [("missing", 404), ("foreign", 403), ("admin", 422)])
def test_plural_checks_subject_again_after_lock(directory, monkeypatch, case, status):
    client, _, subject, first, second, assignments, bumped, *_ = directory
    current = (
        None
        if case == "missing"
        else replace(subject, **({"tenant_id": uuid4()} if case == "foreign" else {"role": "org_admin"}))
    )
    monkeypatch.setattr(local_departments.user_dao, "get", AsyncMock(side_effect=[subject, current]))

    response = client.put(f"/api/users/{subject.id}/departments", json={"department_ids": [str(second.id)]})

    assert response.status_code == status
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()


@pytest.mark.parametrize("selected", [False, True])
def test_platform_admin_plural_requires_explicit_tenant(directory, selected):
    client, actor, subject, first, second, *_ = directory
    actor.role = "platform_admin"
    actor.tenant_id = uuid4()
    query = f"?tenant_id={first.tenant_id}" if selected else ""

    response = client.put(f"/api/users/{subject.id}/departments{query}", json={"department_ids": [str(second.id)]})

    assert response.status_code == (200 if selected else 422)


@pytest.mark.parametrize("role", ["member", "agent_admin"])
def test_plural_assignment_denies_nonadministrator(directory, role):
    client, actor, subject, first, second, assignments, bumped, *_ = directory
    actor.role = role

    response = client.put(f"/api/users/{subject.id}/departments", json={"department_ids": [str(second.id)]})

    assert response.status_code == 403
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()


def test_plural_assignment_requires_authentication(directory):
    client, _, subject, first, _, assignments, bumped, *_ = directory
    client.app.dependency_overrides.clear()

    response = client.put(f"/api/users/{subject.id}/departments", json={"department_ids": []})

    assert response.status_code == 401
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()


def test_plural_assignment_rejects_foreign_tenant_selector(directory):
    client, _, subject, first, _, assignments, bumped, *_ = directory

    response = client.put(f"/api/users/{subject.id}/departments?tenant_id={uuid4()}", json={"department_ids": []})

    assert response.status_code == 403
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()
