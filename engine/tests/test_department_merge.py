from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.security import get_current_user
from app.dao.org_department_dao import DepartmentMergeState
from app.records.identity import IdentityRecord
from app.services import org_departments
from test_department_api import directory

__all__ = ["directory"]


@pytest.fixture
def merge_directory(directory, monkeypatch):
    _client, _actor, subject, source, destination, memberships, _bumped, old_agent, new_agent, rows = directory
    overlap, destination_only, shared_agent = uuid4(), uuid4(), uuid4()
    memberships[subject.id] = {source.id, uuid4()}
    memberships[overlap] = {source.id, destination.id}
    memberships[destination_only] = {destination.id}
    grants = {source.id: {old_agent, shared_agent}, destination.id: {new_agent, shared_agent}}
    events = []

    async def preview(first, second):
        first_members = {user for user, ids in memberships.items() if first.id in ids}
        second_members = {user for user, ids in memberships.items() if second.id in ids}
        return DepartmentMergeState(
            source=first,
            destination=second,
            source_members=frozenset(first_members),
            destination_members=frozenset(second_members),
            source_grants=frozenset(grants[first.id]),
            destination_grants=frozenset(grants[second.id]),
        )

    async def persist(first, second):
        events.append("persist")
        for ids in memberships.values():
            if first.id in ids:
                ids.remove(first.id)
                ids.add(second.id)
        grants[second.id].update(grants.pop(first.id))
        rows.pop(first.id)

    async def members(department):
        return {user for user, ids in memberships.items() if department.id in ids}

    async def agents(department):
        return grants[department.id].copy()

    async def tenants(_db, _ids):
        events.append("tenant")

    async def user(_db, user_id, _tenant):
        events.append(user_id)

    async def agent(_db, agent_id, _tenant):
        events.append(agent_id)

    monkeypatch.setattr(org_departments.org_department_dao, "merge_preview", preview)
    mutation = AsyncMock(side_effect=persist)
    monkeypatch.setattr(org_departments.org_department_dao, "merge_departments", mutation)
    monkeypatch.setattr(org_departments.org_department_dao, "member_user_ids", members)
    monkeypatch.setattr(org_departments.org_department_dao, "grant_agents_for_department", agents)
    monkeypatch.setattr(org_departments, "lock_tenants", tenants)
    monkeypatch.setattr(org_departments, "lock_user", user)
    monkeypatch.setattr(org_departments, "lock_agent", agent)
    audit = AsyncMock()
    monkeypatch.setattr(org_departments, "write_admin_audit", audit)
    return directory, events, mutation, audit, grants


def test_preview_is_read_only_and_reports_overlap(merge_directory):
    directory, events, mutation, audit, _ = merge_directory
    client, actor, _, source, destination, *_ = directory
    response = client.get(f"/api/departments/{source.id}/merge-preview?destination_id={destination.id}")
    assert response.status_code == 200
    assert {
        key: value
        for key, value in response.json().items()
        if key not in {"source", "destination", "confirmation_token"}
    } == {
        "source_membership_count": 2,
        "destination_membership_count": 2,
        "overlapping_membership_count": 1,
        "source_agent_grant_count": 2,
        "destination_agent_grant_count": 2,
        "overlapping_agent_grant_count": 1,
    }
    assert response.json()["source"]["name"] == source.name
    assert response.json()["destination"]["id"] == str(destination.id)
    assert events == ["tenant", actor.id]
    mutation.assert_not_awaited()
    audit.assert_not_awaited()
    directory[6].assert_not_awaited()


def test_apply_locks_subjects_and_union_agents_then_audits(merge_directory):
    directory, events, mutation, audit, grants = merge_directory
    client, actor, subject, source, destination, memberships, bumped, *_, rows = directory
    subjects = sorted(memberships)
    affected = sorted(set.union(*grants.values()))
    unrelated = memberships[subject.id] - {source.id}
    reviewed = client.get(f"/api/departments/{source.id}/merge-preview?destination_id={destination.id}").json()
    events.clear()
    response = client.post(
        f"/api/departments/{source.id}/merge",
        json={"destination_id": str(destination.id), "confirmation_token": reviewed["confirmation_token"]},
    )
    assert response.status_code == 200
    assert response.json()["source_membership_count"] == 2
    assert source.id not in rows
    assert memberships[subject.id] == {destination.id, *unrelated}
    assert all(source.id not in selected for selected in memberships.values())
    assert events == ["tenant", actor.id, *subjects, *affected, "persist"]
    assert [call.args[0] for call in bumped.await_args_list] == affected
    mutation.assert_awaited_once_with(source, destination)
    assert audit.await_args.kwargs["action"] == "department.merge"
    assert audit.await_args.kwargs["details"] == {
        key: value for key, value in response.json().items() if key != "confirmation_token"
    }


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("self", 422),
        ("missing_source", 404),
        ("missing_destination", 404),
        ("foreign", 404),
        ("member", 403),
        ("agent_admin", 403),
        ("foreign_selection", 403),
        ("platform_unselected", 422),
    ],
)
def test_invalid_merge_pair_or_authority_cannot_mutate(merge_directory, case, status, apply):
    directory, _, mutation, audit, _ = merge_directory
    client, actor, _, source, destination, *_ = directory
    source_id, destination_id, tenant = source.id, destination.id, None
    match case:
        case "self":
            destination_id = source_id
        case "missing_source":
            source_id = uuid4()
        case "missing_destination":
            destination_id = uuid4()
        case "foreign":
            directory[-1][destination.id] = replace(destination, tenant_id=uuid4())
        case "member" | "agent_admin":
            actor.role = case
        case "foreign_selection":
            tenant = uuid4()
        case "platform_unselected":
            actor.role = "platform_admin"
    query = "" if tenant is None else f"&tenant_id={tenant}"
    response = (
        client.post(
            f"/api/departments/{source_id}/merge?{query.lstrip('&')}",
            json={"destination_id": str(destination_id), "confirmation_token": "v1:" + "0" * 64},
        )
        if apply
        else client.get(f"/api/departments/{source_id}/merge-preview?destination_id={destination_id}{query}")
    )
    assert response.status_code == status
    mutation.assert_not_awaited()
    audit.assert_not_awaited()
    directory[6].assert_not_awaited()


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("change", ["role", "inactive", "identity", "password", "tenant", "missing"])
def test_merge_rechecks_fresh_administrator(merge_directory, monkeypatch, change, apply):
    directory, _, mutation, audit, _ = merge_directory
    client, actor, _, source, destination, *_ = directory
    fresh = replace(actor)
    match change:
        case "role":
            fresh.role = "member"
        case "inactive":
            fresh.is_active = False
        case "identity":
            fresh.identity = IdentityRecord(id=uuid4(), is_active=False)
        case "password":
            fresh.identity = IdentityRecord(id=uuid4(), must_change_password=True)
        case "tenant":
            fresh.tenant_id = uuid4()
        case "missing":
            fresh = None
    read = AsyncMock(return_value=fresh)
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", read)
    response = (
        client.post(
            f"/api/departments/{source.id}/merge",
            json={"destination_id": str(destination.id), "confirmation_token": "v1:" + "0" * 64},
        )
        if apply
        else client.get(f"/api/departments/{source.id}/merge-preview?destination_id={destination.id}")
    )
    assert response.status_code == 403
    read.assert_awaited_once_with(actor.id, fresh=True)
    mutation.assert_not_awaited()
    audit.assert_not_awaited()


def test_platform_administrator_requires_and_honors_explicit_selection(merge_directory, monkeypatch):
    directory, _, mutation, _, _ = merge_directory
    client, actor, _, source, destination, *_ = directory
    platform = replace(actor, role="platform_admin", tenant_id=uuid4())
    client.app.dependency_overrides[get_current_user] = lambda: platform
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", AsyncMock(return_value=platform))
    reviewed = client.get(
        f"/api/departments/{source.id}/merge-preview?destination_id={destination.id}&tenant_id={source.tenant_id}"
    ).json()
    response = client.post(
        f"/api/departments/{source.id}/merge?tenant_id={source.tenant_id}",
        json={"destination_id": str(destination.id), "confirmation_token": reviewed["confirmation_token"]},
    )
    assert response.status_code == 200
    mutation.assert_awaited_once()


@pytest.mark.parametrize("payload", [{}, {"destination_id": "invalid"}, {"destination_id": None}])
def test_merge_parses_destination_at_http_boundary(merge_directory, payload):
    directory, _, mutation, _, _ = merge_directory
    response = directory[0].post(f"/api/departments/{directory[3].id}/merge", json=payload)
    assert response.status_code == 422
    mutation.assert_not_awaited()
