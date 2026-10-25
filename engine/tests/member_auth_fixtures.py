from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from tests.transaction_pool_fakes import CountingPool

from app.api import auth
from app.core.domain.users import UserRole
from app.db import session
from app.main import app as engine_app
from app.records.identity import IdentityProviderRecord, IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.auth_provider import ExternalUserInfo, FeishuAuthProvider
from app.services.auth_registry import auth_provider_registry
from app.services.registration_service import registration_service
from app.services.sso_service import sso_service


@dataclass(frozen=True, slots=True)
class MemberAuthSetup:
    app: FastAPI
    identity: IdentityRecord
    users: list[UserRecord]
    tenants: list[TenantRecord]
    mint: AsyncMock
    pending: AsyncMock


def member_auth_setup(monkeypatch: pytest.MonkeyPatch, *, count: int = 1) -> MemberAuthSetup:
    timestamp = datetime(2026, 10, 25, tzinfo=UTC)
    identity = IdentityRecord(
        id=uuid4(),
        email="member@example.com",
        password_hash="stored-hash",
        email_verified=True,
        created_at=timestamp,
        updated_at=timestamp,
    )
    tenants = [TenantRecord(id=uuid4(), name=f"Company {i}", slug=f"company-{i}") for i in range(count)]
    users = [
        UserRecord(
            id=uuid4(),
            identity_id=identity.id,
            identity=identity,
            tenant_id=tenant.id,
            display_name="Member",
            role=UserRole.MEMBER,
            created_at=timestamp,
        )
        for tenant in tenants
    ]
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    monkeypatch.setattr(auth.identity_dao, "get_by_login_identifier", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "get", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    monkeypatch.setattr(auth.user_dao, "get_by_identity_id", AsyncMock(return_value=users))
    monkeypatch.setattr(auth.user_dao, "get_with_identity", AsyncMock(return_value=users[0]))
    monkeypatch.setattr(auth.user_dao, "get", AsyncMock(return_value=users[0]))
    monkeypatch.setattr(auth.user_dao, "get_representative_user_for_identity", AsyncMock(return_value=users[0]))
    monkeypatch.setattr(auth.tenant_dao, "get", AsyncMock(return_value=tenants[0]))
    monkeypatch.setattr(auth.tenant_dao, "get_by_ids", AsyncMock(return_value=tenants))
    monkeypatch.setattr(auth, "verify_password_async", AsyncMock(return_value=True))
    mint = AsyncMock(return_value="session-token")
    pending = AsyncMock()
    monkeypatch.setattr(auth, "create_access_token", mint)
    monkeypatch.setattr(auth, "_cache_oauth_pending", pending)
    application = FastAPI()
    application.include_router(engine_app.router)
    return MemberAuthSetup(application, identity, users, tenants, mint, pending)


def oauth_setup(monkeypatch: pytest.MonkeyPatch, setup: MemberAuthSetup) -> tuple[AsyncMock, AsyncMock]:
    provider_record = IdentityProviderRecord(id=uuid4(), provider_type="feishu", name="Feishu")
    provider = FeishuAuthProvider(provider=provider_record)
    info = ExternalUserInfo(provider_type="feishu", provider_user_id="remote-user", email="member@example.com")
    monkeypatch.setattr(provider, "exchange_code_for_token", AsyncMock(return_value={"access_token": "remote"}))
    monkeypatch.setattr(provider, "get_user_info", AsyncMock(return_value=info))
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=provider))
    monkeypatch.setattr(auth.identity_provider_dao, "get_preferred", AsyncMock(return_value=provider_record))
    monkeypatch.setattr("app.services.sso_company_admission.find_pending_sso_user", AsyncMock(return_value=None))
    monkeypatch.setattr(sso_service, "resolve_user_identity", AsyncMock(return_value=setup.users[0]))
    stage = AsyncMock(return_value=False)
    link = AsyncMock()
    monkeypatch.setattr("app.services.sso_company_admission.stage_sso_admission", stage)
    monkeypatch.setattr(sso_service, "link_identity", link)
    monkeypatch.setattr(registration_service, "ensure_web_org_member", AsyncMock())
    pending = auth.OAuthPendingCache(
        provider_type="feishu",
        user_info=auth.OAuthPendingUserInfo(provider_type="feishu", provider_user_id="remote-user", raw_data={}),
        token_data={"access_token": "remote"},
        allowed_tenant_ids=[tenant.id for tenant in setup.tenants],
    )
    monkeypatch.setattr(auth, "_get_oauth_pending", AsyncMock(return_value=pending))
    return stage, link
