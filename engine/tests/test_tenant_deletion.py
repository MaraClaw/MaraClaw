import inspect
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import tenants as tenants_api
from app.dao.tenant_dao import TENANT_DELETE_CLEANUP_SQL, TenantDAO
from app.db.connection import DbConnection
from app.records.tenant import TenantRecord
from app.records.user import UserRecord


def make_user(tenant_id: uuid.UUID, role: str) -> UserRecord:
    return UserRecord(
        id=uuid.uuid4(),
        display_name="Tenant deletion test user",
        role=role,
        tenant_id=tenant_id,
        identity_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("actor_kind", ["own_org_admin", "genesis_org_admin", "foreign_org_admin", "member"])
async def test_delete_tenant_rejects_unauthorized_user_without_querying_or_committing(
    monkeypatch: pytest.MonkeyPatch, preview: bool, actor_kind: str
) -> None:
    tenant_id = uuid.uuid4()
    actor = make_user(
        uuid.uuid4() if actor_kind == "foreign_org_admin" else tenant_id,
        role="member" if actor_kind == "member" else "org_admin",
    )
    actor.is_genesis = actor_kind == "genesis_org_admin"
    get, cleanup, count, audit, fallback = (AsyncMock() for _ in range(5))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", get)
    monkeypatch.setattr(tenants_api, "delete_tenant_and_release_identities", cleanup)
    monkeypatch.setattr(tenants_api.tenant_dao, "deletion_preview", count)
    monkeypatch.setattr(tenants_api, "write_admin_audit", audit)
    monkeypatch.setattr(tenants_api.user_dao, "fallback_tenant_for_identity", fallback)
    handler = tenants_api.get_tenant_deletion_preview if preview else tenants_api.delete_tenant
    with pytest.raises(HTTPException) as error:
        await handler(tenant_id, actor)

    assert error.value.status_code == 403
    for operation in (get, cleanup, count, audit, fallback):
        operation.assert_not_awaited()


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
        await tenants_api.delete_tenant(tenant_id, make_user(tenant_id, role="platform_admin"))

    assert error.value.status_code == 404
    assert called["delete"] == 0


@pytest.mark.asyncio
async def test_delete_tenant_cascade_and_fallback(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    fallback_tenant_id = uuid.uuid4()
    deleted: list[uuid.UUID] = []

    async def fake_get(_id):
        return SimpleNamespace(id=_id, name="Acme")

    async def fake_delete(_id):
        deleted.append(_id)

    async def fake_fallback(identity_id, *, exclude_tenant_id):
        assert exclude_tenant_id == tenant_id
        return fallback_tenant_id

    monkeypatch.setattr(tenants_api.tenant_dao, "get", fake_get)
    monkeypatch.setattr(tenants_api, "delete_tenant_and_release_identities", fake_delete)
    monkeypatch.setattr(tenants_api, "write_admin_audit", AsyncMock())
    monkeypatch.setattr(tenants_api.user_dao, "fallback_tenant_for_identity", fake_fallback)

    response = await tenants_api.delete_tenant(tenant_id, make_user(tenant_id, role="platform_admin"))

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
        await tenants_api.delete_tenant(tenant_id, make_user(tenant_id, role="platform_admin"))


def test_delete_cascade_sql_is_static() -> None:
    source = inspect.getsource(TenantDAO.delete_cascade)
    assert "execute_cleanup" in source
    assert all("%(tid)s" in statement for statement in TENANT_DELETE_CLEANUP_SQL)
    assert 'f"DELETE' not in source
    assert "f'DELETE" not in source


def test_tenant_deletion_respects_shared_member_foreign_keys_without_provider_departments() -> None:
    # Given
    statements = list(TENANT_DELETE_CLEANUP_SQL)
    # When
    positions = [
        next(index for index, sql in enumerate(statements) if sql.startswith(prefix))
        for prefix in (
            "DELETE FROM agent_relationships ",
            "DELETE FROM org_members ",
            "DELETE FROM identity_providers ",
            "DELETE FROM users ",
            "DELETE FROM tenants ",
        )
    ]
    # Then
    assert positions == sorted(positions)
    assert all("org_departments" not in statement for statement in statements)


@pytest.mark.asyncio
async def test_deletion_preview_binds_tenant_id_in_one_read_only_query(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    tenant_id = uuid.uuid4()
    db = AsyncMock(spec=DbConnection)
    db.fetchall.return_value = [{"users_count": 2, "agents_count": 3, "schedules_count": 5}]

    @asynccontextmanager
    async def session() -> AsyncGenerator[AsyncMock]:
        yield db

    dao = TenantDAO()
    monkeypatch.setattr(dao, "session", session)
    # When
    result = await dao.deletion_preview(tenant_id)
    # Then
    assert result == {"users_count": 2, "agents_count": 3, "schedules_count": 5}
    db.fetchall.assert_awaited_once()
    query, params = db.fetchall.call_args.args
    assert query.lstrip().upper().startswith("SELECT")
    assert str(tenant_id) not in query
    assert "%(tid)s" in query
    assert params == {"tid": tenant_id}
    assert not set(query.upper().split()) & {"DELETE", "UPDATE", "INSERT", "LOCK", "TRUNCATE"}
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "own_tenant"), [("member", True), ("member", False), ("org_admin", False)])
async def test_deletion_preview_rejects_scope_before_querying(
    monkeypatch: pytest.MonkeyPatch, role: str, own_tenant: bool
) -> None:
    # Given
    tenant_id = uuid.uuid4()
    actor = make_user(tenant_id if own_tenant else uuid.uuid4(), role)
    get = AsyncMock()
    count = AsyncMock()
    monkeypatch.setattr(tenants_api.tenant_dao, "get", get)
    monkeypatch.setattr(tenants_api.tenant_dao, "deletion_preview", count)
    # When
    with pytest.raises(HTTPException) as error:
        await tenants_api.get_tenant_deletion_preview(tenant_id, actor)
    # Then
    assert error.value.status_code == 403
    get.assert_not_awaited()
    count.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["missing", "system", "default", "identity"])
async def test_deletion_preview_rejects_delete_ineligible_company_before_counting(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    # Given
    tenant_id = uuid.uuid4()
    tenant = TenantRecord(
        id=tenant_id,
        name="Target",
        slug="target",
        is_system=condition == "system",
        is_default_end_user_org=condition == "default",
    )
    actor = make_user(tenant_id, role="platform_admin")
    if condition == "identity":
        actor.identity_id = None
    monkeypatch.setattr(
        tenants_api.tenant_dao, "get", AsyncMock(return_value=None if condition == "missing" else tenant)
    )
    count = AsyncMock()
    monkeypatch.setattr(tenants_api.tenant_dao, "deletion_preview", count)
    # When
    with pytest.raises(HTTPException) as error:
        await tenants_api.get_tenant_deletion_preview(tenant_id, actor)
    # Then
    assert error.value.status_code == (404 if condition == "missing" else 400)
    count.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_rechecks_protection_after_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    tenant_id = uuid.uuid4()
    actor = make_user(tenant_id, role="platform_admin")
    tenant = TenantRecord(id=tenant_id, name="Target", slug="target")
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        tenants_api.tenant_dao,
        "deletion_preview",
        AsyncMock(
            return_value={
                "users_count": 1,
                "agents_count": 0,
                "schedules_count": 0,
            }
        ),
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(tenants_api, "delete_tenant_and_release_identities", cleanup)
    await tenants_api.get_tenant_deletion_preview(tenant_id, actor)
    tenant.is_system = True
    # When
    with pytest.raises(HTTPException) as error:
        await tenants_api.delete_tenant(tenant_id, actor)
    # Then
    assert error.value.status_code == 400
    cleanup.assert_not_awaited()
