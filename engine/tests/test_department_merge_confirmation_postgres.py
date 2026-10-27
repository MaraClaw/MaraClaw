import pytest

from app.core.json_types import json_object_from_response
from app.schemas.department import DepartmentMergePreviewOut
from department_merge_postgres_fixtures import (
    department_store,
    merge_request,
    merge_state,
    merge_store,
    schema_db,
)

__all__ = ["department_store", "merge_store", "schema_db"]


@pytest.mark.parametrize("change", ["membership", "grant", "source_name", "destination_name"])
async def test_reviewed_merge_rejects_equal_count_replacement_or_rename(merge_store, change):
    store, destination_agent, _ = merge_store
    reviewed = await merge_request(store, "preview")
    assert reviewed.status_code == 200
    match change:
        case "membership":
            await store.db.execute(
                "UPDATE local_department_memberships SET department_id = CASE "
                "WHEN department_id = %(source)s THEN %(destination)s::uuid ELSE %(source)s::uuid END "
                "WHERE (user_id = %(first)s AND department_id = %(source)s) "
                "OR (user_id = %(second)s AND department_id = %(destination)s)",
                {
                    "first": store.members[0],
                    "second": store.members[2],
                    "source": store.selected,
                    "destination": store.other,
                },
            )
        case "grant":
            await store.db.execute(
                "UPDATE agent_permissions SET scope_id = CASE "
                "WHEN scope_id = %(source)s THEN %(destination)s::uuid ELSE %(source)s::uuid END "
                "WHERE agent_id IN (%(first)s, %(second)s) AND scope_type = 'department'",
                {
                    "first": store.agents[1],
                    "second": destination_agent,
                    "source": store.selected,
                    "destination": store.other,
                },
            )
        case "source_name" | "destination_name":
            await store.db.execute(
                "UPDATE local_departments SET name = 'Renamed' WHERE id = %(id)s",
                {"id": store.selected if change == "source_name" else store.other},
            )
    before = await merge_state(store)
    current = await merge_request(store, "preview")
    assert {key: value for key, value in json_object_from_response(current).items() if key.endswith("_count")} == {
        key: value for key, value in json_object_from_response(reviewed).items() if key.endswith("_count")
    }
    response = await merge_request(
        store,
        "merge",
        confirmation_token=DepartmentMergePreviewOut.model_validate_json(reviewed.content).confirmation_token,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "department_merge_preview_changed"
    assert await merge_state(store) == before
    assert store.invalidated == []


@pytest.mark.parametrize("change", ["grant_row_identity", "ineligible_grant", "restored_name"])
async def test_confirmation_tracks_canonical_state_not_row_identity_or_history(merge_store, change):
    store, _, _ = merge_store
    reviewed = await merge_request(store, "preview")
    match change:
        case "grant_row_identity":
            await store.db.execute(
                "UPDATE agent_permissions SET id = gen_random_uuid() WHERE scope_type = 'department'"
            )
        case "ineligible_grant":
            await store.db.execute(
                "DELETE FROM agent_permissions WHERE agent_id = %(agent)s AND scope_type = 'department'",
                {"agent": store.agents[2]},
            )
        case "restored_name":
            await store.db.execute(
                "UPDATE local_departments SET name = 'Temporary' WHERE id = %(id)s", {"id": store.selected}
            )
            await store.db.execute(
                "UPDATE local_departments SET name = 'Selected' WHERE id = %(id)s", {"id": store.selected}
            )
    response = await merge_request(
        store,
        "merge",
        confirmation_token=DepartmentMergePreviewOut.model_validate_json(reviewed.content).confirmation_token,
    )
    assert response.status_code == 200, response.text
    assert response.json() == reviewed.json()
    assert set(json_object_from_response(response)) == {
        "source",
        "destination",
        "confirmation_token",
        "source_membership_count",
        "destination_membership_count",
        "overlapping_membership_count",
        "source_agent_grant_count",
        "destination_agent_grant_count",
        "overlapping_agent_grant_count",
    }
