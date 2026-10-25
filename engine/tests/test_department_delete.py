from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.security import get_current_user
from app.records.identity import IdentityRecord
from app.services import org_departments
from test_department_api import directory

__all__ = ["directory"]


@pytest.fixture
def deletion(directory, monkeypatch):
    _client, _actor, subject, first, second, assignments, _bumped, old_agent, new_agent, rows = directory
    assignments[subject.id] = {first.id, second.id}
    affected = {old_agent, new_agent}
    events = []

    async def members(department):
        assert department.id == first.id
        return {subject.id}

    async def agents(department):
        assert department.id in rows
        events.append("capture")
        return affected

    async def cleanup(department):
        events.append("cleanup")
        rows.pop(department.id)
        for ids in assignments.values():
            ids.discard(department.id)

    async def lock_tenants(_db, _ids):
        events.append("tenants")

    async def lock_user(_db, user_id, _tenant):
        events.append(user_id)

    async def lock_agent(_db, agent_id, _tenant):
        events.append(agent_id)

    monkeypatch.setattr(org_departments.org_department_dao, "member_user_ids", members)
    monkeypatch.setattr(org_departments.org_department_dao, "grant_agents_for_department", agents)
    monkeypatch.setattr(org_departments.org_department_dao, "delete_department", cleanup)
    monkeypatch.setattr(org_departments, "lock_tenants", lock_tenants)
    monkeypatch.setattr(org_departments, "lock_user", lock_user)
    monkeypatch.setattr(org_departments, "lock_agent", lock_agent)
    return directory, events


def test_delete_returns_empty_204_and_preserves_other_assignments(deletion):
    # Given two selected memberships and two affected agents.
    directory, events = deletion
    client, actor, subject, first, second, assignments, bumped, old_agent, new_agent, rows = directory
    # When the own-tenant administrator deletes one department.
    response = client.delete(f"/api/departments/{first.id}?tenant_id={actor.tenant_id}")
    # Then capture and ordered locks precede cleanup; only that membership disappears.
    assert response.status_code == 204
    assert response.content == b""
    assert first.id not in rows
    assert second.id in rows
    assert assignments[subject.id] == {second.id}
    assert events == ["tenants", actor.id, subject.id, "capture", *sorted((old_agent, new_agent)), "cleanup"]
    assert [call.args[0] for call in bumped.await_args_list] == sorted((old_agent, new_agent))


@pytest.mark.parametrize("change", ["role", "inactive", "identity", "password", "missing"])
def test_delete_rechecks_fresh_principal_before_cleanup(deletion, monkeypatch, change):
    # Given a stale authorized actor whose persisted authority has changed.
    directory, events = deletion
    client, actor, subject, first, second, assignments, bumped, *_, rows = directory
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
        case "missing":
            fresh = None
    read = AsyncMock(return_value=fresh)
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", read)
    # When deletion is requested with the stale principal.
    response = client.delete(f"/api/departments/{first.id}?tenant_id={actor.tenant_id}")
    # Then fresh authority denies the request without touching directory or ACLs.
    assert response.status_code == 403
    read.assert_awaited_once_with(actor.id, fresh=True)
    assert first.id in rows
    assert assignments[subject.id] == {first.id, second.id}
    assert "cleanup" not in events
    bumped.assert_not_awaited()


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("member", 403),
        ("agent_admin", 403),
        ("pa_unselected", 422),
        ("foreign_selection", 403),
        ("foreign_department", 404),
        ("missing_department", 404),
        ("missing_tenant", 404),
    ],
)
def test_delete_obeys_tenant_boundary_without_mutation(deletion, monkeypatch, case, expected):
    # Given a forbidden actor, selection, or department.
    directory, events = deletion
    client, actor, _, first, _, _, bumped, *_, rows = directory
    selected = actor.tenant_id
    department_id = first.id
    match case:
        case "member" | "agent_admin":
            actor.role = case
        case "pa_unselected":
            actor.role = "platform_admin"
            selected = None
        case "foreign_selection":
            selected = uuid4()
        case "foreign_department":
            rows[first.id] = replace(first, tenant_id=uuid4())
        case "missing_department":
            department_id = uuid4()
        case "missing_tenant":
            monkeypatch.setattr(org_departments.tenant_dao, "get", AsyncMock(return_value=None))
    query = "" if selected is None else f"?tenant_id={selected}"
    # When deletion crosses that boundary.
    response = client.delete(f"/api/departments/{department_id}{query}")
    # Then no department or authorization state changes.
    assert response.status_code == expected
    assert first.id in rows
    assert "cleanup" not in events
    bumped.assert_not_awaited()


def test_platform_admin_can_delete_explicit_target(deletion, monkeypatch):
    # Given a platform administrator selecting a tenant other than their membership.
    directory, _ = deletion
    client, actor, _, first, *_ = directory
    platform_actor = replace(actor, role="platform_admin", tenant_id=uuid4())
    client.app.dependency_overrides[get_current_user] = lambda: platform_actor
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", AsyncMock(return_value=platform_actor))
    # When deletion names the explicit target organization.
    response = client.delete(f"/api/departments/{first.id}?tenant_id={first.tenant_id}")
    # Then the operation succeeds.
    assert response.status_code == 204
