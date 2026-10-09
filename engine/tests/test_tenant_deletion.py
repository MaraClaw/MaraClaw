import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import tenants as tenants_api
from app.dao.tenant_dao import TenantDAO


def make_user(tenant_id: uuid.UUID, role: str = "org_admin"):
    return SimpleNamespace(
        display_name="Tenant deletion test user",
        role=role,
        tenant_id=tenant_id,
        identity_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_delete_tenant_rejects_unauthorized_user_without_querying_or_committing(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    called = {"get": 0, "delete": 0}

    async def fake_get(_id):
        called["get"] += 1
        return SimpleNamespace(id=_id)

    async def fake_delete(_id):
        called["delete"] += 1

    monkeypatch.setattr(tenants_api.tenant_dao, "get", fake_get)
    monkeypatch.setattr(tenants_api.tenant_dao, "delete_cascade", fake_delete)

    with pytest.raises(HTTPException) as error:
        await tenants_api.delete_tenant(tenant_id, make_user(tenant_id, role="member"))

    assert error.value.status_code == 403
    assert called["get"] == 0
    assert called["delete"] == 0


@pytest.mark.asyncio
async def test_delete_tenant_raises_not_found_without_cleanup_or_commit(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    called = {"delete": 0}

    async def fake_get(_id):
        return None

    async def fake_delete(_id):
        called["delete"] += 1

    monkeypatch.setattr(tenants_api.tenant_dao, "get", fake_get)
    monkeypatch.setattr(tenants_api.tenant_dao, "delete_cascade", fake_delete)

    with pytest.raises(HTTPException) as error:
        await tenants_api.delete_tenant(tenant_id, make_user(tenant_id))

    assert error.value.status_code == 404
    assert called["delete"] == 0


@pytest.mark.asyncio
async def test_delete_tenant_cascade_and_fallback(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    fallback_tenant_id = uuid.uuid4()
    deleted: list[uuid.UUID] = []

    async def fake_get(_id):
        return SimpleNamespace(id=_id, name="Acme")

    calls: list[str] = []

    async def fake_delete(_id):
        calls.append("delete")
        deleted.append(_id)

    async def fake_audit(**_kwargs):
        calls.append("audit")

    async def fake_fallback(identity_id, *, exclude_tenant_id):
        assert exclude_tenant_id == tenant_id
        return fallback_tenant_id

    monkeypatch.setattr(tenants_api.tenant_dao, "get", fake_get)
    monkeypatch.setattr(tenants_api, "delete_tenant_and_release_identities", fake_delete)
    monkeypatch.setattr(tenants_api, "write_admin_audit", fake_audit)
    monkeypatch.setattr(tenants_api.user_dao, "fallback_tenant_for_identity", fake_fallback)

    response = await tenants_api.delete_tenant(tenant_id, make_user(tenant_id))

    assert calls == ["audit", "delete"]
    assert deleted == [tenant_id]
    assert response == {"status": "deleted", "fallback_tenant_id": str(fallback_tenant_id)}


@pytest.mark.asyncio
async def test_delete_tenant_does_not_fallback_when_cleanup_fails(monkeypatch) -> None:
    tenant_id = uuid.uuid4()

    async def fake_get(_id):
        return SimpleNamespace(id=_id, name="Acme")

    async def fake_delete(_id):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(tenants_api.tenant_dao, "get", fake_get)
    monkeypatch.setattr(tenants_api, "delete_tenant_and_release_identities", fake_delete)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await tenants_api.delete_tenant(tenant_id, make_user(tenant_id))


def test_delete_cascade_sql_is_static() -> None:
    source = inspect.getsource(TenantDAO.delete_cascade)
    assert "DELETE FROM" in source
    assert 'f"DELETE' not in source
    assert "f'DELETE" not in source
    skill_files = source.index("DELETE FROM skill_files")
    skills = source.index("DELETE FROM skills WHERE")
    clear_created_by = source.index("UPDATE agent_templates SET created_by = NULL")
    clear_updated_by = source.index("UPDATE enterprise_info SET updated_by = NULL")
    users = source.index("DELETE FROM users WHERE tenant_id")
    assert skill_files < skills < users
    assert clear_created_by < users
    assert clear_updated_by < users
    assert "tenant_id = %(tid)s" in source
