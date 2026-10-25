from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import tenants
from app.core import security
from app.core.domain.users import UserRole
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [False, True], ids=["delete", "preview"])
@pytest.mark.parametrize(
    "actor_kind",
    ["platform", "own_platform", "own_org_admin", "genesis_org_admin", "foreign_org_admin", "member"],
)
async def test_tenant_deletion_http_role_matrix(
    monkeypatch: pytest.MonkeyPatch, preview: bool, actor_kind: str
) -> None:
    tenant_id, identity_id, fallback_id = (uuid4() for _ in range(3))
    actor = UserRecord(
        id=uuid4(),
        identity_id=identity_id,
        tenant_id=uuid4() if actor_kind in ("platform", "foreign_org_admin") else tenant_id,
        role=UserRole.PLATFORM_ADMIN
        if actor_kind in ("platform", "own_platform")
        else (UserRole.MEMBER if actor_kind == "member" else UserRole.ORG_ADMIN),
        is_genesis=actor_kind == "genesis_org_admin",
        identity=IdentityRecord(id=identity_id),
    )
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=actor))
    monkeypatch.setattr(security.user_dao, "get", AsyncMock(return_value=actor))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    get = AsyncMock(return_value=TenantRecord(id=tenant_id, name="Target", slug="target"))
    counts = {"users_count": 2, "agents_count": 3, "schedules_count": 5}
    count = AsyncMock(return_value=counts)
    cleanup, audit = AsyncMock(), AsyncMock()
    fallback = AsyncMock(return_value=fallback_id)
    monkeypatch.setattr(tenants.tenant_dao, "get", get)
    monkeypatch.setattr(tenants.tenant_dao, "deletion_preview", count)
    monkeypatch.setattr(tenants, "delete_tenant_and_release_identities", cleanup)
    monkeypatch.setattr(tenants, "write_admin_audit", audit)
    monkeypatch.setattr(tenants.user_dao, "fallback_tenant_for_identity", fallback)
    app = FastAPI()
    app.include_router(tenants.router, prefix="/api")
    token = await security.create_access_token(str(actor.id), actor.role)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            "GET" if preview else "DELETE",
            f"/api/tenants/{tenant_id}" + ("/deletion-preview" if preview else ""),
            headers={"Authorization": f"Bearer {token}"},
        )

    if actor.role == "platform_admin":
        assert response.status_code == 200
        get.assert_awaited_once_with(tenant_id)
        if preview:
            assert response.json() == counts
            count.assert_awaited_once_with(tenant_id)
        else:
            assert response.json() == {"status": "deleted", "fallback_tenant_id": str(fallback_id)}
            cleanup.assert_awaited_once_with(tenant_id)
            audit.assert_awaited_once()
            fallback.assert_awaited_once_with(identity_id, exclude_tenant_id=tenant_id)
    else:
        assert response.status_code == 403
        assert "platform admin" in response.json()["detail"].lower()
        get.assert_not_awaited()
    if preview or actor.role != "platform_admin":
        for operation in (cleanup, audit, fallback):
            operation.assert_not_awaited()
    if not preview or actor.role != "platform_admin":
        count.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [False, True], ids=["delete", "preview"])
@pytest.mark.parametrize("condition", ["missing", "system", "default", "identity", "password", "unauthenticated"])
async def test_tenant_deletion_http_preserves_eligibility_and_authentication_checks(
    monkeypatch: pytest.MonkeyPatch, preview: bool, condition: str
) -> None:
    tenant_id, identity_id = uuid4(), uuid4()
    actor = UserRecord(
        id=uuid4(),
        identity_id=identity_id,
        role=UserRole.PLATFORM_ADMIN,
        identity=IdentityRecord(id=identity_id, must_change_password=condition == "password"),
    )
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=actor))
    monkeypatch.setattr(security.user_dao, "get", AsyncMock(return_value=actor))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    tenant = TenantRecord(
        id=tenant_id,
        name="Target",
        slug="target",
        is_system=condition == "system",
        is_default_end_user_org=condition == "default",
    )
    get = AsyncMock(return_value=None if condition == "missing" else tenant)
    count, cleanup, audit, fallback = (AsyncMock() for _ in range(4))
    monkeypatch.setattr(tenants.tenant_dao, "get", get)
    monkeypatch.setattr(tenants.tenant_dao, "deletion_preview", count)
    monkeypatch.setattr(tenants, "delete_tenant_and_release_identities", cleanup)
    monkeypatch.setattr(tenants, "write_admin_audit", audit)
    monkeypatch.setattr(tenants.user_dao, "fallback_tenant_for_identity", fallback)
    app = FastAPI()
    app.include_router(tenants.router, prefix="/api")
    token = await security.create_access_token(str(actor.id), actor.role)
    if condition == "identity":
        actor.identity_id = None
    headers = {} if condition == "unauthenticated" else {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            "GET" if preview else "DELETE",
            f"/api/tenants/{tenant_id}" + ("/deletion-preview" if preview else ""),
            headers=headers,
        )

    expected_status = {"missing": 404, "password": 403, "unauthenticated": 401, "identity": 401}.get(condition, 400)
    assert response.status_code == expected_status
    if condition == "password":
        assert response.json()["detail"]["must_change_password"] is True
    if condition in ("password", "unauthenticated", "identity"):
        get.assert_not_awaited()
    else:
        get.assert_awaited_once_with(tenant_id)
    for operation in (count, cleanup, audit, fallback):
        operation.assert_not_awaited()
