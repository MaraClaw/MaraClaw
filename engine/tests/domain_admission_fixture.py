from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response

from app.api import auth
from app.core.domain.users import UserRole
from app.dao.company_admission_dao import company_admission_dao
from app.dao.company_transfer_dao import company_transfer_dao
from app.dao.notification_dao import notification_dao
from app.db import session
from app.db.connection import DbConnection
from app.records.company_admission import AdmissionMethod
from app.records.identity import IdentityRecord
from app.records.notification import NotificationRecord
from app.records.tenant import TenantRecord
from app.records.tenant_email_domain import TenantEmailDomainRecord
from app.records.user import UserRecord
from app.services import org_membership, system_email_service
from app.services.email_verification_service import email_verification_service
from app.services.registration_service import registration_service
from company_transfer_fakes import AdmissionStore, TransferStore
from test_db_session import _FakePool, _FakeRawConnection


@dataclass(slots=True)
class DomainAdmissionFixture:
    source: TenantRecord
    target: TenantRecord
    other: TenantRecord
    applicant: UserRecord
    identity: IdentityRecord
    genesis: UserRecord
    users: dict[UUID, UserRecord]
    admissions: AdmissionStore
    transfers: TransferStore
    raw: _FakeRawConnection
    registered: bool = False
    owns_agents: bool = False
    notices: list[NotificationRecord] = field(default_factory=list)
    tokens: set[str] = field(default_factory=lambda: {"verify-token"})
    client: AsyncClient = field(init=False)

    async def register(self, *, invitation_code: str | None = None) -> Response:
        return await self.client.post(
            "/api/auth/register/init",
            json={"email": self.identity.email, "password": "secure-password", "invitation_code": invitation_code},
        )

    async def verify(self) -> Response:
        return await self.client.post("/api/auth/verify-email", json={"token": "verify-token"})


@pytest.fixture
async def domain_admission_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DomainAdmissionFixture]:
    source = TenantRecord(id=uuid4(), name="KABERs", slug="kabers", is_default_end_user_org=True)
    target = TenantRecord(id=uuid4(), name="Claimed Company", slug="claimed")
    other = TenantRecord(id=uuid4(), name="Invited Company", slug="invited")
    created_at = datetime(2026, 10, 24, tzinfo=UTC)
    identity = IdentityRecord(
        id=uuid4(),
        email="member@claimed.example",
        password_hash="hash",
        email_verified=True,
        created_at=created_at,
        updated_at=created_at,
    )
    applicant = UserRecord(
        id=uuid4(),
        tenant_id=source.id,
        identity_id=identity.id,
        identity=identity,
        created_at=created_at,
    )
    genesis = UserRecord(id=uuid4(), tenant_id=target.id, role=UserRole.ORG_ADMIN, is_genesis=True)
    other_genesis = UserRecord(id=uuid4(), tenant_id=other.id, role=UserRole.ORG_ADMIN, is_genesis=True)
    admissions = AdmissionStore([source, target, other])
    fixture = DomainAdmissionFixture(
        source,
        target,
        other,
        applicant,
        identity,
        genesis,
        {user.id: user for user in (applicant, genesis, other_genesis)},
        admissions,
        TransferStore(admissions),
        _FakeRawConnection(),
    )

    async def memberships(_identity_id: UUID) -> list[UserRecord]:
        return [applicant] if fixture.registered else []

    async def create_user(*, tenant_id: UUID | None, **_kwargs: object) -> UserRecord:
        fixture.registered = True
        applicant.tenant_id = tenant_id
        return applicant

    async def get_user(user_id: UUID, **_kwargs: object) -> UserRecord | None:
        return fixture.users.get(user_id)

    async def update_user(*, db_obj: UserRecord, obj_in: Mapping[str, object]) -> UserRecord:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def update_identity(*, db_obj: IdentityRecord, obj_in: Mapping[str, object]) -> IdentityRecord:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def consume(token: str) -> dict[str, object] | None:
        if token not in fixture.tokens:
            return None
        fixture.tokens.remove(token)
        return {"identity_id": identity.id, "email": identity.email}

    async def domain_claim(domain: str) -> TenantEmailDomainRecord | None:
        return (
            TenantEmailDomainRecord(id=uuid4(), tenant_id=target.id, domain=domain)
            if domain == "claimed.example"
            else None
        )

    async def fetchone(
        _db: DbConnection,
        sql: str,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        assert params is not None
        if "FROM tenants" in sql:
            return {"id": params["id"]}
        if "role='org_admin'" in sql:
            reviewer = next(
                (
                    user
                    for user in fixture.users.values()
                    if user.tenant_id == params["id"]
                    and user.role == "org_admin"
                    and user.is_genesis
                    and user.is_active
                ),
                None,
            )
            return {"id": reviewer.id} if reviewer else None
        if "FROM users" in sql:
            user = next((user for user in fixture.users.values() if user.id == params["id"]), None)
            return {"tenant_id": user.tenant_id} if user else None
        raise AssertionError(sql)

    async def fetchval(_db: DbConnection, sql: str, params: Mapping[str, object] | None = None) -> bool:
        if "FROM company_admission_requests" in sql:
            assert params is not None
            return any(
                row.applicant_user_id == params["id"] and row.method == AdmissionMethod.INVITATION
                for row in fixture.admissions.rows.values()
            )
        return fixture.owns_agents

    async def notification(*, obj_in: Mapping[str, object]) -> NotificationRecord:
        record = NotificationRecord.from_row({"id": uuid4(), **obj_in})
        fixture.notices.append(record)
        return record

    token = session._conn_ctx.set(None)
    monkeypatch.setattr(session, "get_pool", lambda: _FakePool(fixture.raw))
    monkeypatch.setattr(DbConnection, "fetchone", fetchone)
    monkeypatch.setattr(DbConnection, "fetchval", fetchval)
    monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "get", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "update", update_identity)
    monkeypatch.setattr(auth.user_dao, "get_by_identity_id", memberships)
    monkeypatch.setattr(auth.user_dao, "get_with_identity", get_user)
    monkeypatch.setattr(auth.user_dao, "update", update_user)
    monkeypatch.setattr(auth.user_dao, "get_representative_user_for_identity", AsyncMock(return_value=applicant))
    monkeypatch.setattr(auth.tenant_dao, "get", admissions.tenant)
    monkeypatch.setattr(auth.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=source))
    monkeypatch.setattr(org_membership.tenant_email_domain_dao, "get_by_domain", domain_claim)
    monkeypatch.setattr(registration_service, "find_or_create_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(registration_service, "create_user_with_identity", create_user)
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=None))
    monkeypatch.setattr(auth, "hash_password_async", AsyncMock(return_value="hash"))
    monkeypatch.setattr(auth, "verify_password_async", AsyncMock(return_value=True))
    monkeypatch.setattr(auth, "create_access_token", AsyncMock(return_value="access-token"))
    monkeypatch.setattr(auth, "_send_verification_email_task", AsyncMock())
    monkeypatch.setattr(email_verification_service, "consume_email_verification_token", consume)
    monkeypatch.setattr(notification_dao, "create", notification)
    for name in ("tenant", "pending_for", "create"):
        monkeypatch.setattr(company_admission_dao, name, getattr(admissions, name))
    for name in ("active_for", "for_admission"):
        monkeypatch.setattr(company_transfer_dao, name, getattr(fixture.transfers, name))
    app = FastAPI()
    app.include_router(auth.router, prefix="/api", dependencies=[Depends(session.bind_crud_connection)])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            fixture.client = client
            yield fixture
    finally:
        app.dependency_overrides.clear()
        session._conn_ctx.reset(token)
