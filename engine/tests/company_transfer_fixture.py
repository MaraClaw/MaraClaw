from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import anyio
import pytest

from app.api import background_tasks
from app.core.domain.users import UserRole
from app.core.notification_kinds import NotificationKind
from app.db import session
from app.db.connection import DbConnection
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.transfer_policy import TransferPolicy
from app.services import (
    company_admissions as admission_service,
    company_transfer_guards as guards,
    company_transfer_requests as requests,
    company_transfer_review as review,
)
from company_transfer_fakes import AdmissionStore, TransferStore
from test_db_session import _FakePool, _FakeRawConnection


@dataclass(slots=True)
class TransferFixture:
    source: TenantRecord
    target: TenantRecord
    applicant: UserRecord
    reviewer: UserRecord
    genesis: UserRecord
    admissions: AdmissionStore
    transfers: TransferStore
    raw: _FakeRawConnection
    users: dict[UUID, UserRecord]
    owned_agents: bool = True
    notices: list[tuple[UUID, NotificationKind]] = field(default_factory=list)
    emails: list[tuple[str, int]] = field(default_factory=list)
    email_ready: anyio.Event = field(default_factory=anyio.Event)


@pytest.fixture
async def transfer_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TransferFixture]:
    source = TenantRecord(id=uuid4(), name="KABERs", slug="kabers", is_default_end_user_org=True)
    target = TenantRecord(id=uuid4(), name="Target", slug="target")
    identity = IdentityRecord(id=uuid4(), email="applicant@example.test", email_verified=True)
    applicant = UserRecord(id=uuid4(), tenant_id=source.id, identity_id=identity.id, identity=identity)
    reviewer = UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)
    genesis = UserRecord(id=uuid4(), tenant_id=target.id, role=UserRole.ORG_ADMIN, is_genesis=True)
    admissions = AdmissionStore([source, target])
    raw = _FakeRawConnection()
    fixture = TransferFixture(
        source,
        target,
        applicant,
        reviewer,
        genesis,
        admissions,
        TransferStore(admissions),
        raw,
        {user.id: user for user in (applicant, reviewer, genesis)},
    )

    async def fresh(user_id: UUID, *, require_active: bool = True) -> UserRecord:
        user = fixture.users[user_id]
        if require_active and not user.is_active:
            from fastapi import HTTPException

            raise HTTPException(403, "Inactive membership")
        return user

    async def lock_tenants(_db: DbConnection, _ids: Sequence[UUID | None]) -> None:
        return None

    async def lock_user(_db: DbConnection, _id: UUID, _tenant: UUID | None) -> None:
        return None

    async def fetchone(_db: DbConnection, _sql: str, _params: MappingParams = None) -> dict[str, object]:
        return {"id": genesis.id}

    async def has_owned_agents(_user: UserRecord) -> bool:
        return fixture.owned_agents

    async def validate(*_args: object, **_kwargs: object) -> None:
        return None

    async def fallback() -> TenantRecord:
        return source

    async def policy() -> TransferPolicy:
        return TransferPolicy(fee_amount_minor=3700, currency="USD", instructions="Offline payment reference")

    async def notice(user_id: UUID, *, notification_kind: NotificationKind, **_kwargs: object) -> None:
        fixture.notices.append((user_id, notification_kind))

    async def email(*, to: str, subject: str, body: str) -> None:
        del subject, body
        fixture.emails.append((to, raw.commits))
        fixture.email_ready.set()

    token = session._conn_ctx.set(None)
    background_tasks.start_background_tasks()
    monkeypatch.setattr(session, "get_pool", lambda: _FakePool(raw))
    monkeypatch.setattr(DbConnection, "fetchone", fetchone)
    for module in (admission_service, requests, review):
        monkeypatch.setattr(module, "fresh_user", fresh)
        monkeypatch.setattr(module, "lock_tenants", lock_tenants)
        monkeypatch.setattr(module, "lock_user", lock_user)
        monkeypatch.setattr(module, "transfers", fixture.transfers)
    for module in (admission_service, requests, guards):
        monkeypatch.setattr(module, "admissions", admissions)
    monkeypatch.setattr(guards, "transfers", fixture.transfers)
    monkeypatch.setattr(guards, "has_owned_agents", has_owned_agents)
    monkeypatch.setattr(admission_service, "validate_method", validate)
    monkeypatch.setattr(requests, "validate_method", validate)
    monkeypatch.setattr(requests, "get_transfer_policy", policy)
    monkeypatch.setattr(guards, "get_transfer_policy", policy)
    monkeypatch.setattr(review, "get_fallback_org", fallback)
    monkeypatch.setattr(admission_service, "send_notification", notice)
    monkeypatch.setattr(review, "send_notification", notice)
    monkeypatch.setattr(review, "send_system_email", email)
    try:
        yield fixture
    finally:
        await background_tasks.drain_background_tasks(grace_seconds=0)
        background_tasks.start_background_tasks()
        session._conn_ctx.reset(token)


type MappingParams = dict[str, object] | None
