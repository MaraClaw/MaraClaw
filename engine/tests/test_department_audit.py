from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.records.identity import IdentityRecord
from app.services import admin_audit, org_departments
from test_department_api import directory

__all__ = ["directory"]


@pytest.fixture
def audit_sink(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    sink = AsyncMock()
    monkeypatch.setattr(admin_audit.admin_audit_log_dao, "create", sink)
    return sink


def test_creation_records_fresh_actor_and_trimmed_allowlisted_name(directory, monkeypatch, audit_sink):
    # Given a stale actor and a freshly authorized platform administrator.
    client, actor, *_ = directory
    fresh = replace(
        actor,
        role="platform_admin",
        identity=IdentityRecord(id=uuid4(), email="fresh@example.test", password_hash="never-audit-this"),
    )
    read = AsyncMock(return_value=fresh)
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", read)
    # When the create route normalizes its input.
    response = client.post(f"/api/departments?tenant_id={actor.tenant_id}", json={"name": "  Finance  "})
    # Then exactly one allowlisted event identifies the fresh actor and created object.
    assert response.status_code == 201
    read.assert_awaited_once_with(actor.id, fresh=True)
    audit_sink.assert_awaited_once()
    assert audit_sink.await_args.kwargs["obj_in"] == {
        "actor_id": actor.id,
        "actor_role": "platform_admin",
        "actor_email": "fresh@example.test",
        "action": "department.create",
        "target_type": "department",
        "target_id": next(row.id for row in directory[-1].values() if row.name == "Finance"),
        "tenant_id": actor.tenant_id,
        "changes": {"name": {"before": None, "after": "Finance"}},
        "details": {},
        "ip_address": None,
    }


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_department_before_name_survives_mutating_dao(directory, monkeypatch, audit_sink, operation):
    # Given a DAO object that mutates in place, including an unrelated sensitive attribute.
    client, actor, _, first, *_, rows = directory
    mutable = SimpleNamespace(
        id=first.id, tenant_id=first.tenant_id, name=first.name, created_at=first.created_at, token="never-audit-this"
    )
    rows[first.id] = mutable
    fresh = replace(actor, identity=IdentityRecord(id=uuid4(), email="fresh@example.test"))
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", AsyncMock(return_value=fresh))

    async def update(*, db_obj, obj_in):
        db_obj.name = obj_in["name"]
        return db_obj

    async def delete(department):
        department.name = "removed"
        rows.pop(department.id)

    monkeypatch.setattr(org_departments.org_department_dao, "update", update)
    monkeypatch.setattr(org_departments.org_department_dao, "delete_department", delete)
    monkeypatch.setattr(org_departments.org_department_dao, "member_user_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        org_departments.org_department_dao, "grant_agents_for_department", AsyncMock(return_value=set())
    )
    # When the service owner mutates the department through its real route.
    if operation == "rename":
        response = client.patch(f"/api/departments/{first.id}", json={"name": "  Revenue  "})
    else:
        response = client.delete(f"/api/departments/{first.id}")
    # Then the event retains the original name rather than the mutated object or its extra fields.
    assert response.status_code == (200 if operation == "rename" else 204)
    audit_sink.assert_awaited_once()
    event = audit_sink.await_args.kwargs["obj_in"]
    assert event["action"] == f"department.{operation}"
    assert event["changes"] == {"name": {"before": "Sales", "after": "Revenue" if operation == "rename" else None}}
    assert event["target_id"] == first.id
    assert event["tenant_id"] == actor.tenant_id
    assert event["actor_email"] == "fresh@example.test"
    assert event["details"] == {}


@pytest.mark.parametrize("route", ["department", "departments"])
@pytest.mark.parametrize("clear", [False, True])
def test_assignment_routes_emit_one_normalized_replacement(directory, monkeypatch, audit_sink, route, clear):
    # Given two memberships and another user's unrelated assignment.
    client, actor, subject, first, second, assignments, *_ = directory
    before_ids = [first.id] if route == "departments" and not clear else [first.id, second.id]
    assignments[subject.id] = set(before_ids)
    assignments[uuid4()] = {first.id}
    fresh = replace(actor, identity=IdentityRecord(id=uuid4(), email="fresh@example.test"))
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", AsyncMock(return_value=fresh))
    selected = [] if clear else ([second.id, first.id] if route == "departments" else [second.id])
    payload = (
        {"department_id": None if clear else str(second.id)}
        if route == "department"
        else {"department_ids": [str(value) for value in selected * 2]}
    )
    # When either compatibility or plural route replaces the complete set.
    response = client.put(f"/api/users/{subject.id}/{route}", json=payload)
    # Then exactly one service-owned event contains normalized before/after UUIDs.
    assert response.status_code == 200
    audit_sink.assert_awaited_once()
    event = audit_sink.await_args.kwargs["obj_in"]
    assert event["action"] == "department.membership.replace"
    assert event["target_type"] == "user"
    assert event["target_id"] == subject.id
    assert event["tenant_id"] == actor.tenant_id
    assert event["actor_email"] == "fresh@example.test"
    assert event["changes"] == {
        "department_ids": {
            "before": [str(value) for value in sorted(before_ids)],
            "after": [str(value) for value in sorted(selected)],
        }
    }
    assert event["details"] == {}


@pytest.mark.parametrize("operation", ["rename", "department", "departments", "clear"])
def test_ineffective_operations_emit_no_event_or_acl_bump(directory, audit_sink, operation):
    # Given an already matching normalized value.
    client, _, subject, first, _, assignments, bumped, *_ = directory
    if operation == "clear":
        assignments.clear()
    # When the route receives the same effective name or set.
    match operation:
        case "rename":
            response = client.patch(f"/api/departments/{first.id}", json={"name": "  Sales  "})
        case "department":
            response = client.put(f"/api/users/{subject.id}/department", json={"department_id": str(first.id)})
        case _:
            response = client.put(
                f"/api/users/{subject.id}/departments",
                json={"department_ids": [] if operation == "clear" else [str(first.id), str(first.id)]},
            )
    # Then normalized replays succeed without producing activity or invalidating ACLs.
    assert response.status_code == 200
    audit_sink.assert_not_awaited()
    bumped.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "rename", "delete", "department", "departments"])
@pytest.mark.parametrize("authority", ["role", "inactive", "password", "tenant"])
def test_fresh_authority_rejection_emits_no_audit(directory, monkeypatch, audit_sink, operation, authority):
    # Given a stale authorized actor whose persisted principal no longer permits this operation.
    client, actor, subject, first, second, assignments, bumped, *_, rows = directory
    fresh = replace(actor)
    match authority:
        case "role":
            fresh.role = "member"
        case "inactive":
            fresh.is_active = False
        case "password":
            fresh.identity = IdentityRecord(id=uuid4(), must_change_password=True)
        case "tenant":
            fresh.tenant_id = uuid4()
    read = AsyncMock(return_value=fresh)
    monkeypatch.setattr(org_departments.user_dao, "get_with_identity", read)
    # When a write is requested using the stale actor.
    match operation:
        case "create":
            response = client.post("/api/departments", json={"name": "Denied"})
        case "rename":
            response = client.patch(f"/api/departments/{first.id}", json={"name": "Denied"})
        case "delete":
            response = client.delete(f"/api/departments/{first.id}")
        case "department":
            response = client.put(f"/api/users/{subject.id}/department", json={"department_id": str(second.id)})
        case _:
            response = client.put(f"/api/users/{subject.id}/departments", json={"department_ids": [str(second.id)]})
    # Then fresh checks reject before any audit or mutation.
    assert response.status_code == 403
    read.assert_awaited_once_with(actor.id, fresh=True)
    audit_sink.assert_not_awaited()
    assert assignments[subject.id] == {first.id}
    assert rows[first.id].name == "Sales"
    assert len(rows) == 2
    bumped.assert_not_awaited()


@pytest.mark.parametrize("route", ["department", "departments"])
def test_foreign_department_rejects_entire_replacement_without_audit(directory, audit_sink, route):
    # Given a department in another tenant alongside a valid selection.
    client, _, subject, first, second, assignments, bumped, *_, rows = directory
    rows[second.id] = replace(second, tenant_id=uuid4())
    payload = (
        {"department_id": str(second.id)}
        if route == "department"
        else {"department_ids": [str(first.id), str(second.id), str(second.id)]}
    )
    # When either assignment route is asked to cross the tenant boundary.
    response = client.put(f"/api/users/{subject.id}/{route}", json=payload)
    # Then all memberships are preserved, with no audit or invalidation.
    assert response.status_code == 422
    assert assignments[subject.id] == {first.id}
    audit_sink.assert_not_awaited()
    bumped.assert_not_awaited()
