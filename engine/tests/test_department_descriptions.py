from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.services import org_departments
from test_department_api import directory

__all__ = ["directory"]


@pytest.mark.parametrize("description", ["", " \t\n ", "  Billing\nEscalations\t "])
def test_create_and_list_preserve_exact_description(directory, description):
    client, _, _, _, _, _, bumped, *_, rows = directory

    response = client.post("/api/departments", json={"name": "Finance", "description": description})

    assert response.status_code == 201
    assert response.json()["description"] == description
    assert next(row.description for row in rows.values() if row.name == "Finance") == description
    listed = client.get("/api/departments")
    assert listed.status_code == 200
    assert next(row["description"] for row in listed.json()["departments"] if row["name"] == "Finance") == description
    bumped.assert_not_awaited()


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [({}, "  Sales responsibilities\n"), ({"description": ""}, ""), ({"description": " \t\n "}, " \t\n ")],
)
@pytest.mark.parametrize("rename", [False, True])
def test_patch_preserves_omission_and_persists_exact_supplied_description(directory, metadata, expected, rename):
    client, _, subject, first, _, assignments, bumped, *_, rows = directory
    name = "Revenue" if rename else first.name

    response = client.patch(f"/api/departments/{first.id}", json={"name": name, **metadata})

    assert response.status_code == 200
    assert response.json()["description"] == expected
    assert rows[first.id] == replace(first, name=name, description=expected)
    assert assignments == {subject.id: {first.id}}
    bumped.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "patch"])
@pytest.mark.parametrize("description", [None, 7, ["text"], {"text": "value"}])
def test_description_rejects_non_text_at_http_boundary(directory, operation, description):
    client, _, _, first, _, _, bumped, *_, rows = directory
    before = dict(rows)
    payload = {"name": "Finance", "description": description}

    response = (
        client.post("/api/departments", json=payload)
        if operation == "create"
        else client.patch(f"/api/departments/{first.id}", json=payload)
    )

    assert response.status_code == 422
    assert rows == before
    bumped.assert_not_awaited()


def test_description_does_not_make_patch_name_optional(directory):
    client, _, _, first, *_, rows = directory

    response = client.patch(f"/api/departments/{first.id}", json={"description": "Responsibilities"})

    assert response.status_code == 422
    assert rows[first.id] == first


def test_unchanged_supplied_description_is_a_noop(directory, monkeypatch):
    client, _, _, first, _, _, bumped, *_ = directory
    update, audit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(org_departments.org_department_dao, "update", update)
    monkeypatch.setattr(org_departments, "write_admin_audit", audit)

    response = client.patch(f"/api/departments/{first.id}", json={"name": first.name, "description": first.description})

    assert response.status_code == 200
    assert response.json()["description"] == first.description
    update.assert_not_awaited()
    audit.assert_not_awaited()
    bumped.assert_not_awaited()
