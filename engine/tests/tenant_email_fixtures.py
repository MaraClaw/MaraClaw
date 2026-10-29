from contextlib import nullcontext
from dataclasses import dataclass, field
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from app.api import enterprise, notification, tenant_email
from app.core.domain.users import UserRole
from app.core.json_types import JsonObject
from app.core.security import get_current_user
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.tenant_email import TenantEmailUpdate
from app.services import email_config, tenant_email_admin


@dataclass
class SMTPAdmin:
    actor: UserRecord
    fresh: UserRecord
    tenant_id: UUID
    app: FastAPI
    tenants: dict[UUID, TenantRecord]
    saved: dict[UUID, JsonObject] = field(default_factory=dict)
    audits: list[dict[str, object]] = field(default_factory=list)

    def configure_maraclaw(self) -> UUID:
        tenant = next(row for row in self.tenants.values() if row.slug == "maraclaw")
        self.saved[tenant.id] = email_config.stored_settings(
            TenantEmailUpdate(enabled=True, smtp_host="maraclaw.example.com", from_address="platform@example.com"),
            {},
            public_only=False,
        )
        return tenant.id


@pytest.fixture
def smtp_admin(monkeypatch: pytest.MonkeyPatch) -> SMTPAdmin:
    own = TenantRecord(uuid4(), "Company", "company")
    mara = TenantRecord(uuid4(), "MaraClaw", "maraclaw", is_system=True)
    kabers = TenantRecord(uuid4(), "KABERs", "kabers", is_system=True)
    identity = IdentityRecord(id=uuid4(), email="admin@example.com")
    actor = UserRecord(id=uuid4(), tenant_id=own.id, role=UserRole.ORG_ADMIN, identity=identity, is_genesis=True)
    app = FastAPI()
    app.include_router(tenant_email.router, prefix="/api")
    app.include_router(enterprise.router, prefix="/api")
    app.include_router(notification.router, prefix="/api")
    state = SMTPAdmin(actor, actor, own.id, app, {row.id: row for row in (own, mara, kabers)})
    app.dependency_overrides[get_current_user] = lambda: state.actor

    async def get(tenant_id: UUID) -> JsonObject:
        return state.saved.get(tenant_id, {}).copy()

    async def put(tenant_id: UUID, value: JsonObject) -> None:
        state.saved[tenant_id] = value.copy()

    async def delete(tenant_id: UUID) -> None:
        state.saved.pop(tenant_id, None)

    async def audit(**kwargs: object) -> None:
        state.audits.append(kwargs)

    monkeypatch.setattr(tenant_email_admin, "connection_ctx", lambda: nullcontext())
    monkeypatch.setattr(tenant_email_admin, "lock_tenants", AsyncMock())
    monkeypatch.setattr(tenant_email_admin, "lock_user", AsyncMock())
    monkeypatch.setattr(
        tenant_email_admin.user_dao, "get_with_identity", AsyncMock(side_effect=lambda *_a, **_k: state.fresh)
    )
    monkeypatch.setattr(tenant_email_admin.tenant_dao, "get", AsyncMock(side_effect=lambda key: state.tenants.get(key)))
    monkeypatch.setattr(
        tenant_email_admin.tenant_dao,
        "get_by_slug",
        AsyncMock(side_effect=lambda slug: next((row for row in state.tenants.values() if row.slug == slug), None)),
    )
    monkeypatch.setattr(email_config.tenant_email_dao, "get", get)
    monkeypatch.setattr(email_config.tenant_email_dao, "put", put)
    monkeypatch.setattr(email_config.tenant_email_dao, "delete", delete)
    monkeypatch.setattr(tenant_email_admin, "write_admin_audit", audit)
    monkeypatch.setattr(tenant_email_admin, "resolve_public_addresses", AsyncMock(return_value=("8.8.8.8",)))
    return state
