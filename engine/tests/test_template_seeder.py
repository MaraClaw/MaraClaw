"""Folder-only agent template catalog."""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.services import template_seeder

_RETIRED_PYTHON_NAMES = frozenset(
    {
        "Project Manager",
        "Designer",
        "Product Intern",
        "Market Researcher",
    }
)


def test_seeder_loads_only_folder_templates() -> None:
    templates = template_seeder._load_folder_templates()
    names = {template["name"] for template in templates}

    assert not hasattr(template_seeder, "DEFAULT_TEMPLATES")
    assert len(templates) == 22
    assert names.isdisjoint(_RETIRED_PYTHON_NAMES)
    assert all(template["soul_template"] for template in templates)


async def test_seeder_unlinks_then_deletes_retired_builtins(monkeypatch) -> None:
    retired_id = uuid.uuid4()
    kept = {
        "name": "Chief of Staff",
        "description": "kept",
        "icon": "CS",
        "category": "office",
        "is_builtin": True,
        "capability_bullets": [],
        "bootstrap_content": None,
        "soul_template": "# soul",
        "default_skills": [],
        "default_autonomy_policy": {},
    }
    existing_kept = SimpleNamespace(id=uuid.uuid4(), name="Chief of Staff")
    existing_retired = SimpleNamespace(id=retired_id, name="Project Manager")
    cleared: list[uuid.UUID] = []
    deleted: list[uuid.UUID] = []
    updated: list[object] = []

    class FakeDao:
        async def list_builtins(self):
            return [existing_kept, existing_retired]

        async def clear_agent_references(self, template_id):
            cleared.append(template_id)
            return 2

        async def delete(self, *, id):
            deleted.append(id)
            return existing_retired

        async def get_builtin_by_name(self, name):
            return existing_kept if name == "Chief of Staff" else None

        async def update(self, *, db_obj, obj_in):
            updated.append((db_obj, obj_in))
            return db_obj

        async def create(self, *, obj_in):
            raise AssertionError("must not insert retired Python templates")

    @asynccontextmanager
    async def fake_connection_ctx():
        yield None

    monkeypatch.setattr(template_seeder, "_load_folder_templates", lambda: [kept])
    monkeypatch.setattr(template_seeder, "agent_template_dao", FakeDao())
    monkeypatch.setattr(template_seeder, "connection_ctx", fake_connection_ctx)

    await template_seeder.seed_agent_templates()

    assert cleared == [retired_id]
    assert deleted == [retired_id]
    assert updated
    assert updated[0][0] is existing_kept
