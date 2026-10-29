from uuid import uuid4

import pytest
from psycopg.errors import NotNullViolation

from app.core.json_types import json_object_from_response
from app.db.errors import DbError
from department_merge_postgres_fixtures import department_store, merge_client, merge_state, schema_db

__all__ = ["department_store", "schema_db"]


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [({}, ""), ({"description": " \t\n "}, " \t\n "), ({"description": None}, None)],
)
async def test_create_description_round_trips_http_and_postgres(department_store, metadata, expected):
    store = department_store
    async with merge_client(store) as client:
        created = await client.post("/api/departments", json={"name": "Created", **metadata})
        listed = await client.get("/api/departments")

    assert created.status_code == (422 if expected is None else 201), created.text
    persisted = await store.db.fetchall("SELECT description FROM local_departments WHERE name = 'Created'")
    assert persisted == ([] if expected is None else [{"description": expected}])
    assert listed.status_code == 200
    matching = [row for row in listed.json()["departments"] if row["name"] == "Created"]
    assert [row["description"] for row in matching] == ([] if expected is None else [expected])
    assert store.invalidated == []


@pytest.mark.parametrize("rename", [False, True])
@pytest.mark.parametrize(
    ("metadata", "expected", "status"),
    [
        ({}, "  Selected responsibilities\n", 200),
        ({"description": ""}, "", 200),
        ({"description": " \t\n "}, " \t\n ", 200),
        ({"description": None}, "  Selected responsibilities\n", 422),
    ],
)
async def test_patch_description_preserves_associations_and_acl(department_store, metadata, expected, status, rename):
    store = department_store
    before = await merge_state(store)
    name = "Renamed" if rename else "Selected"

    async with merge_client(store) as client:
        response = await client.patch(f"/api/departments/{store.selected}", json={"name": name, **metadata})

    assert response.status_code == status, response.text
    persisted = await store.db.fetchone(
        "SELECT name, description FROM local_departments WHERE id = %(id)s", {"id": store.selected}
    )
    assert persisted == {"name": name if status == 200 else "Selected", "description": expected}
    if status == 200:
        assert json_object_from_response(response)["description"] == expected
    after = await merge_state(store)
    assert after[1:3] == before[1:3]
    assert store.invalidated == []


async def test_database_description_default_and_null_constraint(department_store):
    store = department_store
    await store.db.execute(
        "INSERT INTO local_departments (id, tenant_id, name) VALUES (%(id)s, %(tenant)s, 'Defaulted')",
        {"id": uuid4(), "tenant": store.tenant},
    )
    assert await store.db.fetchone("SELECT description FROM local_departments WHERE name = 'Defaulted'") == {
        "description": ""
    }

    with pytest.raises(DbError) as rejected:
        await store.db.execute(
            "INSERT INTO local_departments (id, tenant_id, name, description) VALUES (%(id)s, %(tenant)s, 'Null', NULL)",
            {"id": uuid4(), "tenant": store.tenant},
        )
    assert isinstance(rejected.value.orig, NotNullViolation)
    assert rejected.value.orig.diag.column_name == "description"
