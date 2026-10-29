from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.schemas.department import DepartmentMergeSelection
from app.services import org_departments
from test_department_merge import directory, merge_directory

__all__ = ["directory", "merge_directory"]


@pytest.mark.parametrize(
    "token", ["missing", None, "", "v1:short", "v2:" + "0" * 64, "v1:" + "g" * 64, 123, "v1:" + "0" * 64 + "\n"]
)
def test_apply_rejects_missing_or_malformed_confirmation_at_http_boundary(merge_directory, token):
    directory, _, mutation, audit, _ = merge_directory
    client, _, _, source, destination, *_ = directory
    payload = {"destination_id": str(destination.id)}
    if token != "missing":
        payload["confirmation_token"] = token
    response = client.post(f"/api/departments/{source.id}/merge", json=payload)
    assert response.status_code == 422
    mutation.assert_not_awaited()
    audit.assert_not_awaited()
    directory[6].assert_not_awaited()


@pytest.mark.parametrize("token", [None, "", "v1:short", "v2:" + "0" * 64])
async def test_direct_service_apply_also_requires_a_valid_confirmation(merge_directory, token):
    directory, events, mutation, audit, _ = merge_directory
    _, actor, _, source, destination, *_ = directory
    selection = DepartmentMergeSelection(source.id, destination.id, source.tenant_id)
    with pytest.raises(HTTPException) as rejected:
        await org_departments.merge_departments(actor, selection, apply=True, confirmation_token=token)
    assert rejected.value.status_code == 422
    assert events == []
    mutation.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        "source_member",
        "destination_member",
        "source_grant",
        "destination_grant",
        "source_name",
        "destination_name",
        "source_description",
        "destination_description",
        "reverse",
        "tenant",
    ],
)
def test_apply_rejects_reviewed_identity_or_selection_change_before_effects(merge_directory, change):
    directory, events, mutation, audit, grants = merge_directory
    client, _, subject, source, destination, memberships, bumped, *_, rows = directory
    reviewed = client.get(f"/api/departments/{source.id}/merge-preview?destination_id={destination.id}").json()
    source_id, destination_id = source.id, destination.id
    match change:
        case "source_member":
            memberships[uuid4()] = memberships.pop(subject.id)
        case "destination_member":
            member = next(user for user, ids in memberships.items() if ids == {destination.id})
            memberships[uuid4()] = memberships.pop(member)
        case "source_grant" | "destination_grant":
            selected, other = (source.id, destination.id) if change == "source_grant" else (destination.id, source.id)
            grants[selected].remove(next(iter(grants[selected] - grants[other])))
            grants[selected].add(uuid4())
        case "source_name" | "destination_name":
            department = source if change == "source_name" else destination
            rows[department.id] = replace(department, name="Renamed")
        case "source_description" | "destination_description":
            department = source if change == "source_description" else destination
            rows[department.id] = replace(department, description=department.description + " ")
        case "reverse":
            source_id, destination_id = destination.id, source.id
        case "tenant":
            tenant = uuid4()
            directory[1].tenant_id = tenant
            rows[source.id] = replace(source, tenant_id=tenant)
            rows[destination.id] = replace(destination, tenant_id=tenant)
    events.clear()
    response = client.post(
        f"/api/departments/{source_id}/merge",
        json={"destination_id": str(destination_id), "confirmation_token": reviewed["confirmation_token"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "department_merge_preview_changed"
    assert events[0:2] == ["tenant", directory[1].id]
    mutation.assert_not_awaited()
    audit.assert_not_awaited()
    bumped.assert_not_awaited()


def test_confirmation_survives_order_changes_and_a_name_restored_to_reviewed_state(merge_directory):
    directory, _, mutation, _, _ = merge_directory
    client, _, _, source, destination, memberships, *_ = directory
    reviewed = client.get(f"/api/departments/{source.id}/merge-preview?destination_id={destination.id}").json()
    reordered = dict(reversed(list(memberships.items())))
    memberships.clear()
    memberships.update(reordered)
    directory[-1][source.id] = replace(source, name="Temporary")
    directory[-1][source.id] = replace(source)
    response = client.post(
        f"/api/departments/{source.id}/merge",
        json={"destination_id": str(destination.id), "confirmation_token": reviewed["confirmation_token"]},
    )
    assert response.status_code == 200
    assert response.json()["confirmation_token"] == reviewed["confirmation_token"]
    mutation.assert_awaited_once()


async def test_comparison_reads_state_only_after_member_and_agent_locks(merge_directory, monkeypatch):
    directory, _, mutation, audit, grants = merge_directory
    _, actor, _, source, destination, *_ = directory
    selection = DepartmentMergeSelection(source.id, destination.id, source.tenant_id)
    reviewed = await org_departments.merge_departments(actor, selection, apply=False)

    async def change_during_agent_lock(_db, _agent_id, _tenant):
        grants[source.id] = {uuid4()}

    monkeypatch.setattr(org_departments, "lock_agent", change_during_agent_lock)
    with pytest.raises(HTTPException) as rejected:
        await org_departments.merge_departments(
            actor, selection, apply=True, confirmation_token=reviewed.confirmation_token
        )
    assert rejected.value.status_code == 409
    mutation.assert_not_awaited()
    audit.assert_not_awaited()
