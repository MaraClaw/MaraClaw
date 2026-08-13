import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.api import skills as skills_api
from app.core.security import get_current_user
from app.main import app
from app.records.skill import SkillRecord


@pytest.fixture
def org_admin_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        role="org_admin",
        tenant_id=uuid.uuid4(),
        is_active=True,
        department_id=None,
    )


@pytest.fixture
def platform_admin_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        role="platform_admin",
        tenant_id=uuid.uuid4(),
        is_active=True,
        department_id=None,
    )


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)

    async def _build():
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _build


@pytest.mark.asyncio
async def test_org_admin_can_delete_custom_skill_via_browse(monkeypatch, client, org_admin_user):
    skill = SkillRecord(
        id=uuid.uuid4(),
        name="Tenant Skill",
        folder_name="tenant-skill",
        tenant_id=org_admin_user.tenant_id,
        is_builtin=False,
        files=[],
    )
    deleted: list[uuid.UUID] = []

    async def fake_get(*args, **kwargs):
        return skill

    async def fake_delete(skill_id):
        deleted.append(skill_id)

    monkeypatch.setattr(skills_api.skill_dao, "get_by_folder_for_tenant_scope", fake_get)
    monkeypatch.setattr(skills_api.skill_dao, "delete_with_files", fake_delete)
    app.dependency_overrides[get_current_user] = lambda: org_admin_user

    async with await client() as ac:
        response = await ac.delete("/api/skills/browse/delete", params={"path": "tenant-skill"})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert deleted == [skill.id]


@pytest.mark.asyncio
async def test_org_admin_can_delete_custom_skill_directly(monkeypatch, client, org_admin_user):
    skill = SkillRecord(
        id=uuid.uuid4(),
        name="Tenant Skill",
        folder_name="tenant-skill",
        tenant_id=org_admin_user.tenant_id,
        is_builtin=False,
    )
    deleted: list[uuid.UUID] = []

    async def fake_get(*args, **kwargs):
        return skill

    async def fake_delete(skill_id):
        deleted.append(skill_id)

    monkeypatch.setattr(skills_api.skill_dao, "get_for_tenant_scope", fake_get)
    monkeypatch.setattr(skills_api.skill_dao, "delete_with_files", fake_delete)
    app.dependency_overrides[get_current_user] = lambda: org_admin_user

    async with await client() as ac:
        response = await ac.delete(f"/api/skills/{skill.id}")

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert deleted == [skill.id]


@pytest.mark.asyncio
async def test_browse_write_creates_tenant_skill_without_iterating_lazy_files(monkeypatch, client, platform_admin_user):
    created: dict = {}

    async def fake_get(*args, **kwargs):
        return None

    async def fake_create(*, obj_in):
        rec = SkillRecord(
            id=uuid.uuid4(),
            name=obj_in["name"],
            folder_name=obj_in["folder_name"],
            tenant_id=obj_in.get("tenant_id"),
            description=obj_in.get("description") or "",
            category=obj_in.get("category") or "custom",
            icon=obj_in.get("icon") or "--",
            is_builtin=False,
            files=[],
        )
        created["skill"] = rec
        return rec

    async def fake_file_create(*, obj_in):
        created["file"] = obj_in
        return SimpleNamespace(**obj_in, id=uuid.uuid4())

    monkeypatch.setattr(skills_api.skill_dao, "get_by_folder_for_tenant_scope", fake_get)
    monkeypatch.setattr(skills_api.skill_dao, "create", fake_create)
    monkeypatch.setattr(skills_api.skill_file_dao, "create", fake_file_create)
    app.dependency_overrides[get_current_user] = lambda: platform_admin_user

    async with await client() as ac:
        response = await ac.put(
            "/api/skills/browse/write",
            json={"path": "tenant-skill/SKILL.md", "content": "# test"},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert created["skill"].folder_name == "tenant-skill"
    assert created["skill"].tenant_id == platform_admin_user.tenant_id
    assert created["file"]["path"] == "SKILL.md"
    assert created["file"]["content"] == "# test"
