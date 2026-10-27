from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.core import access_cache
from app.core.json_types import json_object_from_response
from app.core.permissions import check_agent_access
from app.dao.org_department_dao import org_department_dao
from app.db.session import transaction
from app.records.user import UserRecord
from app.schemas.department import DepartmentMembersOut, DepartmentMergePreviewOut
from app.services import org_departments
from department_merge_postgres_fixtures import (
    department_store,
    merge_client,
    merge_request,
    merge_state,
    merge_store,
    schema_db,
)

__all__ = ["department_store", "merge_store", "schema_db"]


async def test_merge_http_recomputes_preview_and_preserves_permission_and_roster_reads(merge_store):
    store, destination_agent, unrelated = merge_store
    before = await merge_state(store)
    preview = await merge_request(store, "preview")
    assert preview.status_code == 200
    assert [
        preview.json()[name]
        for name in (
            "source_membership_count",
            "destination_membership_count",
            "overlapping_membership_count",
            "source_agent_grant_count",
            "destination_agent_grant_count",
            "overlapping_agent_grant_count",
        )
    ] == [2, 2, 1, 2, 2, 1]
    assert await merge_state(store) == before
    with pytest.raises(HTTPException) as denied:
        await check_agent_access(UserRecord(id=store.members[0]), destination_agent, fresh=True)
    assert denied.value.status_code == 403
    await store.db.execute(
        "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) "
        "VALUES (%(user)s, %(tenant)s, %(department)s)",
        {"user": store.members[2], "tenant": store.tenant, "department": store.selected},
    )
    renamed = await merge_request(store, "destination_rename")
    assert renamed.status_code == 200

    changed = await merge_state(store)
    rejected = await merge_request(
        store,
        "merge",
        confirmation_token=DepartmentMergePreviewOut.model_validate_json(preview.content).confirmation_token,
    )
    assert rejected.status_code == 409
    assert await merge_state(store) == changed
    assert store.invalidated == []
    response = await merge_request(store, "merge")

    assert response.status_code == 200, response.text
    assert response.json()["source_membership_count"] == 3
    assert response.json()["overlapping_membership_count"] == 2
    assert response.json()["destination"]["name"] == "Renamed"
    after = await merge_state(store)
    assert {row["id"] for row in after[0]} == {store.other, unrelated}
    assert {(row["user_id"], row["department_id"]) for row in after[1]} == {
        *((member, store.other) for member in store.members),
        (store.members[0], unrelated),
    }
    assert {(row["agent_id"], row["scope_type"], row["scope_id"], row["access_level"]) for row in after[2]} == {
        (store.agents[0], "department", store.other, "use"),
        (store.agents[1], "department", store.other, "use"),
        (destination_agent, "department", store.other, "use"),
        (store.agents[0], "user", store.members[2], "use"),
        (store.agents[2], "company", None, "use"),
    }
    surviving_ids = {row["id"] for row in before[2] if row["scope_id"] != store.selected}
    assert surviving_ids <= {row["id"] for row in after[2]}
    assert set(store.invalidated) == {*store.agents, destination_agent}
    assert len(store.invalidated) == 4
    event = await store.db.fetchone(
        "SELECT actor_id, target_id, tenant_id, details FROM admin_audit_logs WHERE action = 'department.merge'"
    )
    assert event == {
        "actor_id": store.actor.id,
        "target_id": store.selected,
        "tenant_id": store.tenant,
        "details": {
            key: value for key, value in json_object_from_response(response).items() if key != "confirmation_token"
        },
    }
    assert (await check_agent_access(UserRecord(id=store.members[0]), destination_agent, fresh=True))[1] == "use"
    assert (await check_agent_access(UserRecord(id=store.members[2]), store.agents[1], fresh=True))[1] == "use"
    member_store = replace(store, actor=UserRecord(id=store.members[0], tenant_id=store.tenant))
    async with merge_client(member_store) as client:
        roster = await client.get(f"/api/departments/{store.other}/members")
        retired = await client.get(f"/api/departments/{store.selected}/members")
        forbidden = await client.post(
            f"/api/departments/{unrelated}/merge",
            json={"destination_id": str(store.other), "confirmation_token": preview.json()["confirmation_token"]},
        )
    assert roster.status_code == 200
    assert {member.membership_id for member in DepartmentMembersOut.model_validate_json(roster.content).members} == set(
        store.members
    )
    assert retired.status_code == 404
    assert forbidden.status_code == 403


async def test_merge_defers_union_acl_and_visibility_until_outer_commit(merge_store):
    store, destination_agent, _ = merge_store
    before = await merge_state(store)
    async with transaction():
        response = await merge_request(store, "merge")
        assert response.status_code == 200
        assert await merge_state(store) == before
        assert store.invalidated == []
    assert set(store.invalidated) == {*store.agents, destination_agent}
    assert await merge_state(store) != before


@pytest.mark.parametrize("stage", ["copied", "cleanup", "acl", "outer"])
async def test_merge_failure_rolls_back_associations_audit_and_deferred_acl(merge_store, monkeypatch, stage):
    store, _, _ = merge_store
    before = await merge_state(store)
    original_cleanup = org_department_dao.delete_department

    async def fail_cleanup(department):
        if stage == "cleanup":
            await original_cleanup(department)
        raise RuntimeError("injected merge failure")

    async def fail_acl(agent_id: UUID):
        await access_cache.bump_agent_acl_version(agent_id)
        raise RuntimeError("injected merge failure")

    match stage:
        case "copied" | "cleanup":
            monkeypatch.setattr(org_department_dao, "delete_department", fail_cleanup)
        case "acl":
            monkeypatch.setattr(org_departments, "bump_agent_acl_version", fail_acl)

    async def abort_outer_transaction():
        async with transaction():
            response = await merge_request(store, "merge")
            assert response.status_code == 200
            raise RuntimeError("injected merge failure")

    if stage == "outer":
        with pytest.raises(RuntimeError, match="injected merge failure"):
            await abort_outer_transaction()
    else:
        response = await merge_request(store, "merge")
        assert response.status_code == 500
    assert await merge_state(store) == before
    assert store.invalidated == []


@pytest.mark.parametrize("table", ["local_department_memberships", "agent_permissions"])
async def test_merge_real_insert_error_preserves_all_prior_associations(merge_store, table):
    store, _, _ = merge_store
    before = await merge_state(store)
    if table == "local_department_memberships":
        await store.db.execute(
            "ALTER TABLE local_department_memberships ADD CONSTRAINT reject_merge_membership "
            f"CHECK (department_id <> '{store.other}' OR user_id <> '{store.members[0]}')"
        )
    else:
        await store.db.execute(
            "ALTER TABLE agent_permissions ADD CONSTRAINT reject_merge_grant "
            f"CHECK (scope_id <> '{store.other}' OR agent_id <> '{store.agents[1]}')"
        )
    response = await merge_request(store, "merge")
    assert response.status_code == 500
    assert await merge_state(store) == before
    assert store.invalidated == []


@pytest.mark.parametrize("change", ["role", "tenant", "membership", "identity", "password"])
async def test_merge_http_rejects_stale_persisted_administrator(merge_store, change):
    store, _, _ = merge_store
    reviewed = await merge_request(store, "preview")
    match change:
        case "role":
            await store.db.execute("UPDATE users SET role = 'member' WHERE id = %(id)s", {"id": store.actor.id})
        case "tenant":
            await store.db.execute(
                "UPDATE users SET tenant_id = %(tenant)s WHERE id = %(id)s",
                {"id": store.actor.id, "tenant": store.foreign},
            )
        case "membership":
            await store.db.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": store.actor.id})
        case "identity":
            await store.db.execute("UPDATE identities SET is_active = false WHERE id = %(id)s", {"id": store.identity})
        case "password":
            await store.db.execute(
                "UPDATE identities SET must_change_password = true WHERE id = %(id)s", {"id": store.identity}
            )
    before = await merge_state(store)
    response = await merge_request(
        store,
        "merge",
        confirmation_token=DepartmentMergePreviewOut.model_validate_json(reviewed.content).confirmation_token,
    )
    assert response.status_code == (409 if change == "tenant" else 403)
    assert await merge_state(store) == before
    assert store.invalidated == []


@pytest.mark.parametrize("apply", [False, True])
async def test_merge_rejects_foreign_destination_without_cross_tenant_writes(merge_store, apply):
    store, _, _ = merge_store
    foreign_department = uuid4()
    await store.db.execute(
        "INSERT INTO local_departments (id, tenant_id, name) VALUES (%(id)s, %(tenant)s, 'Foreign')",
        {"id": foreign_department, "tenant": store.foreign},
    )
    before = await merge_state(store)
    async with merge_client(store) as client:
        response = (
            await client.post(
                f"/api/departments/{store.selected}/merge",
                json={"destination_id": str(foreign_department), "confirmation_token": "v1:" + "0" * 64},
            )
            if apply
            else await client.get(
                f"/api/departments/{store.selected}/merge-preview?destination_id={foreign_department}"
            )
        )
    assert response.status_code == 404
    assert await merge_state(store) == before
    assert store.invalidated == []


async def test_merge_audit_failure_is_savepoint_isolated(merge_store):
    store, destination_agent, _ = merge_store
    await store.db.execute(
        "ALTER TABLE admin_audit_logs ADD CONSTRAINT reject_merge_audit CHECK (action <> 'department.merge')"
    )
    response = await merge_request(store, "merge")
    assert response.status_code == 200
    assert {row["id"] for row in await store.db.fetchall("SELECT id FROM local_departments")} == {
        store.other,
        merge_store[2],
    }
    assert await store.db.fetchall("SELECT id FROM admin_audit_logs") == []
    assert set(store.invalidated) == {*store.agents, destination_agent}
