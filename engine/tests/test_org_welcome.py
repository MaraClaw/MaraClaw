"""Private approval welcomes stay out of general registration and membership moves."""

import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import final
from unittest.mock import AsyncMock

import pytest

from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services import org_membership as membership
from app.services.org_welcome import send_organization_welcome
from app.services.registration_service import RegistrationService
from app.services.system_email_service import SystemEmailConfig

SMTP = SystemEmailConfig(
    from_address="noreply@example.com",
    from_name="MaraClaw",
    smtp_host="smtp.example.com",
    smtp_port=587,
    smtp_username="u",
    smtp_password="p",
    smtp_ssl=False,
    smtp_timeout_seconds=10,
)


def _tenant(*, name: str = "Acme", is_active: bool = True, tenant_id: uuid.UUID | None = None) -> TenantRecord:
    return TenantRecord(id=tenant_id or uuid.uuid4(), name=name, slug="acme", is_active=is_active)


def _user(
    *,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    role: str = "member",
    is_active: bool = True,
    identity: IdentityRecord | None = None,
) -> UserRecord:
    return UserRecord(
        id=user_id or uuid.uuid4(), tenant_id=tenant_id, role=role, is_active=is_active, identity=identity
    )


def _created(obj_in: Mapping[str, object]) -> UserRecord:
    return replace(UserRecord(id=uuid.uuid4()), **obj_in)


@pytest.fixture
def send(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    sent = AsyncMock()
    monkeypatch.setattr("app.services.org_welcome.notification_service.send_notification", sent)
    return sent


async def test_welcome_targets_only_the_member_membership(send: AsyncMock):
    tenant = _tenant()
    user = _user()

    await send_organization_welcome(user, tenant)

    kwargs = send.await_args_list[0].kwargs
    assert kwargs["user_id"] == user.id
    assert kwargs["type"] == "organization_welcome"
    assert kwargs["title"] == "Welcome to Acme"
    assert kwargs["body"]
    assert kwargs["ref_id"] == tenant.id
    assert kwargs.get("agent_id") is None
    assert kwargs.get("link") is None


async def test_welcome_title_is_limited_to_200_characters(send: AsyncMock):
    await send_organization_welcome(_user(), _tenant(name="A" * 400))

    assert send.await_args_list[0].kwargs["title"] == "Welcome to " + "A" * 189


@pytest.mark.parametrize(
    "admin",
    [
        _user(role="platform_admin"),
        _user(identity=IdentityRecord(id=uuid.uuid4(), is_platform_admin=True)),
    ],
    ids=["role", "identity"],
)
async def test_welcome_skips_platform_admins(send: AsyncMock, admin: UserRecord):
    assert await send_organization_welcome(admin, _tenant()) is None
    send.assert_not_awaited()


async def test_welcome_failure_is_not_swallowed(send: AsyncMock):
    send.side_effect = RuntimeError("insert failed")

    with pytest.raises(RuntimeError, match="insert failed"):
        await send_organization_welcome(_user(), _tenant())


def _registration(
    monkeypatch: pytest.MonkeyPatch, tenant: TenantRecord | None
) -> tuple[RegistrationService, AsyncMock]:
    welcome = AsyncMock()

    async def _create(*, obj_in: Mapping[str, object]) -> UserRecord:
        return _created(obj_in)

    service = RegistrationService()
    monkeypatch.setattr("app.services.registration_service.tenant_dao.get", AsyncMock(return_value=tenant))
    monkeypatch.setattr("app.services.registration_service.user_dao.create", _create)
    monkeypatch.setattr(service, "bind_org_member", AsyncMock())
    monkeypatch.setattr("app.services.registration_service.participant_dao.create_for_user", AsyncMock())
    monkeypatch.setattr("app.services.org_welcome.send_organization_welcome", welcome)
    return service, welcome


@pytest.mark.parametrize("verified", [True, False], ids=["verified", "unverified-inactive"])
async def test_new_registration_sends_no_welcome(monkeypatch: pytest.MonkeyPatch, verified: bool):
    tenant = _tenant()
    identity = IdentityRecord(id=uuid.uuid4(), email="a@b.com", email_verified=verified)
    service, welcome = _registration(monkeypatch, tenant)

    user = await service.create_user_with_identity(identity=identity, tenant_id=tenant.id, email_config=SMTP)

    assert user.is_active is verified
    welcome.assert_not_awaited()


async def test_registration_without_organization_sends_no_welcome(monkeypatch: pytest.MonkeyPatch):
    identity = IdentityRecord(id=uuid.uuid4(), email="a@b.com", email_verified=True)
    service, welcome = _registration(monkeypatch, None)

    await service.create_user_with_identity(identity=identity, email_config=SMTP)

    welcome.assert_not_awaited()


@final
class _Move:
    """Locked `_set_user_org` harness; `fresh` is the row seen after taking the locks."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fresh: UserRecord, target: TenantRecord):
        self.welcome: AsyncMock = AsyncMock()
        self.update: AsyncMock = AsyncMock(side_effect=lambda *, db_obj, obj_in: replace(db_obj, **obj_in))
        self.unbind: AsyncMock = AsyncMock()
        self.tenants: dict[uuid.UUID, TenantRecord] = {target.id: target}

        @asynccontextmanager
        async def _ctx():
            yield object()

        monkeypatch.setattr(membership, "connection_ctx", _ctx)
        monkeypatch.setattr("app.core.acl_locks.lock_tenants", AsyncMock())
        monkeypatch.setattr("app.core.acl_locks.lock_user", AsyncMock())
        monkeypatch.setattr(membership.user_dao, "get", AsyncMock(return_value=fresh))
        monkeypatch.setattr(membership.tenant_dao, "get", AsyncMock(side_effect=self.tenants.get))
        monkeypatch.setattr("app.services.company_transfer_guards.has_owned_agents", AsyncMock(return_value=False))
        monkeypatch.setattr(membership.user_dao, "update", self.update)
        monkeypatch.setattr(membership.org_member_dao, "unbind_user_from_tenant", self.unbind)
        monkeypatch.setattr("app.services.registration_service.registration_service.bind_org_member", AsyncMock())
        monkeypatch.setattr("app.services.org_welcome.send_organization_welcome", self.welcome)


async def test_direct_join_sends_no_welcome(monkeypatch: pytest.MonkeyPatch):
    target = _tenant()
    stale = _user()
    move = _Move(monkeypatch, _user(user_id=stale.id), target)

    attached = await membership.attach_user_to_org(stale, target)

    assert attached.tenant_id == target.id
    move.welcome.assert_not_awaited()


async def test_direct_transfer_sends_no_welcome(monkeypatch: pytest.MonkeyPatch):
    source = uuid.uuid4()
    target = _tenant()
    stale = _user(tenant_id=source)
    move = _Move(monkeypatch, _user(user_id=stale.id, tenant_id=source), target)
    move.tenants[source] = replace(_tenant(tenant_id=source), is_default_end_user_org=True)

    transferred = await membership.transfer_user_to_org(stale, target)

    assert transferred.tenant_id == target.id
    move.unbind.assert_awaited_once_with(stale.id, source)
    move.welcome.assert_not_awaited()


async def test_nondefault_source_transfer_is_rejected_without_welcome(monkeypatch: pytest.MonkeyPatch):
    # Given a member of a company rather than the default organization.
    source = _tenant()
    target = _tenant()
    member = _user(tenant_id=source.id)
    move = _Move(monkeypatch, member, target)
    move.tenants[source.id] = source

    # When the locked membership transfer checks its source.
    with pytest.raises(membership.AlreadyInOrgError):
        await membership.transfer_user_to_org(member, target)

    # Then neither membership nor welcome delivery changes.
    move.update.assert_not_awaited()
    move.unbind.assert_not_awaited()
    move.welcome.assert_not_awaited()


async def test_stale_same_target_move_is_a_no_op_without_welcome(monkeypatch: pytest.MonkeyPatch):
    # Given a concurrent request already moved the member into the target.
    source = uuid.uuid4()
    target = _tenant()
    stale = _user(tenant_id=source)
    move = _Move(monkeypatch, _user(user_id=stale.id, tenant_id=target.id), target)

    # When this stale request acquires the locks.
    result = await membership.transfer_user_to_org(stale, target)

    # Then it observes the placement and writes nothing.
    assert result.tenant_id == target.id
    move.update.assert_not_awaited()
    move.unbind.assert_not_awaited()
    move.welcome.assert_not_awaited()


async def test_already_in_target_transfer_sends_no_welcome(monkeypatch: pytest.MonkeyPatch):
    target = _tenant()
    member = _user(tenant_id=target.id)
    move = _Move(monkeypatch, member, target)

    assert await membership.transfer_user_to_org(member, target) is member
    move.welcome.assert_not_awaited()


@pytest.mark.parametrize(
    ("fresh", "target_active", "error"),
    [
        (_user(is_active=False), True, membership.AlreadyInOrgError),
        (_user(role="org_admin"), True, membership.AlreadyInOrgError),
        (_user(), False, membership.DefaultOrgUnavailableError),
    ],
    ids=["inactive", "protected-admin", "target-disabled"],
)
async def test_blocked_move_sends_no_welcome(
    monkeypatch: pytest.MonkeyPatch, fresh: UserRecord, target_active: bool, error: type[Exception]
):
    target = _tenant()
    move = _Move(monkeypatch, fresh, _tenant(is_active=target_active, tenant_id=target.id))

    with pytest.raises(error):
        await membership.attach_user_to_org(_user(user_id=fresh.id), target)

    move.update.assert_not_awaited()
    move.welcome.assert_not_awaited()


async def test_disabled_target_is_refused_before_any_welcome(monkeypatch: pytest.MonkeyPatch):
    move = _Move(monkeypatch, _user(), _tenant())

    with pytest.raises(membership.DefaultOrgUnavailableError):
        await membership.attach_user_to_org(_user(), _tenant(is_active=False))

    move.welcome.assert_not_awaited()


async def test_direct_join_does_not_depend_on_welcome_delivery(monkeypatch: pytest.MonkeyPatch):
    target = _tenant()
    move = _Move(monkeypatch, _user(), target)
    move.welcome.side_effect = RuntimeError("insert failed")

    attached = await membership.attach_user_to_org(_user(), target)

    assert attached.tenant_id == target.id
    move.welcome.assert_not_awaited()
