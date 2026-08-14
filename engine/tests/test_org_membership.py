"""Email-domain lookup and one-org placement."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import org_membership as membership


def _tenant(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Acme",
        "slug": "acme",
        "is_active": True,
        "is_default_end_user_org": False,
        "default_message_limit": 50,
        "default_message_period": "permanent",
        "default_max_agents": 2,
        "default_agent_ttl_hours": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme.COM", "acme.com"),
        ("https://Acme.com/path", "acme.com"),
    ],
)
def test_normalize_email_domain_accepts_host(raw, expected):
    assert membership.normalize_email_domain(raw) == expected


@pytest.mark.parametrize("raw", ["", "*", "@acme.com", "com", "not a domain", "http://"])
def test_normalize_email_domain_rejects_junk(raw):
    with pytest.raises(membership.InvalidEmailDomainError):
        membership.normalize_email_domain(raw)


@pytest.mark.asyncio
async def test_place_new_registration_confirms_claimed_domain(monkeypatch):
    acme = _tenant()
    openclaw = _tenant(name="OpenClaw", slug="openclaw", is_default_end_user_org=True)
    claim = SimpleNamespace(tenant_id=acme.id)
    monkeypatch.setattr(membership.tenant_email_domain_dao, "get_by_domain", AsyncMock(return_value=claim))
    monkeypatch.setattr(membership.tenant_dao, "get", AsyncMock(return_value=acme))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=openclaw))

    placed = await membership.place_new_registration(email="ada@acme.com")
    assert placed.needs_org_confirm is True
    assert placed.tenant_id is None
    assert placed.suggested.id == acme.id


@pytest.mark.asyncio
async def test_place_new_registration_autojoins_openclaw(monkeypatch):
    openclaw = _tenant(name="OpenClaw", slug="openclaw", is_default_end_user_org=True)
    monkeypatch.setattr(membership.tenant_email_domain_dao, "get_by_domain", AsyncMock(return_value=None))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=openclaw))

    placed = await membership.place_new_registration(email="bob@gmail.com")
    assert placed.needs_org_confirm is False
    assert placed.tenant_id == openclaw.id


@pytest.mark.asyncio
async def test_place_new_registration_invite_autojoins(monkeypatch):
    invited = _tenant()
    openclaw = _tenant(name="OpenClaw", slug="openclaw", is_default_end_user_org=True)
    code = SimpleNamespace(tenant_id=invited.id, used_count=0, max_uses=5)
    monkeypatch.setattr(membership.invitation_code_dao, "get_active_by_code", AsyncMock(return_value=code))
    monkeypatch.setattr(membership.tenant_dao, "get", AsyncMock(return_value=invited))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=openclaw))

    placed = await membership.place_new_registration(email="x@y.com", invitation_code="ABCD")
    assert placed.needs_org_confirm is False
    assert placed.tenant_id == invited.id


@pytest.mark.asyncio
async def test_invalid_invite_is_invitation_error_not_503(monkeypatch):
    openclaw = _tenant(name="OpenClaw", slug="openclaw", is_default_end_user_org=True)
    monkeypatch.setattr(membership.invitation_code_dao, "get_active_by_code", AsyncMock(return_value=None))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=openclaw))
    with pytest.raises(membership.InvitationError):
        await membership.place_new_registration(email="x@y.com", invitation_code="NOPE")


@pytest.mark.asyncio
async def test_exhausted_invite_is_invitation_error(monkeypatch):
    openclaw = _tenant(name="OpenClaw", slug="openclaw", is_default_end_user_org=True)
    code = SimpleNamespace(tenant_id=uuid.uuid4(), used_count=5, max_uses=5)
    monkeypatch.setattr(membership.invitation_code_dao, "get_active_by_code", AsyncMock(return_value=code))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=openclaw))
    with pytest.raises(membership.InvitationError):
        await membership.place_new_registration(invitation_code="USED")


@pytest.mark.asyncio
async def test_attach_user_to_org_refuses_second_membership():
    user = SimpleNamespace(tenant_id=uuid.uuid4())
    with pytest.raises(membership.AlreadyInOrgError):
        await membership.attach_user_to_org(user, _tenant())


@pytest.mark.asyncio
async def test_transfer_user_to_org_moves_member(monkeypatch):
    source = uuid.uuid4()
    dest = _tenant()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=source, role="member", is_genesis=False)
    updated = SimpleNamespace(id=user.id, tenant_id=dest.id, role="member")
    unbind = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield None

    monkeypatch.setattr(membership.user_dao, "update", AsyncMock(return_value=updated))
    monkeypatch.setattr(membership.org_member_dao, "unbind_user_from_tenant", unbind)
    monkeypatch.setattr("app.services.registration_service.registration_service.bind_org_member", AsyncMock())
    monkeypatch.setattr(membership, "connection_ctx", _ctx)

    result = await membership.transfer_user_to_org(user, dest)
    assert result.tenant_id == dest.id
    unbind.assert_awaited_once_with(user.id, source)


@pytest.mark.asyncio
async def test_attach_user_to_org_blocks_platform_admin():
    user = SimpleNamespace(tenant_id=None, role="platform_admin", is_genesis=False)
    with pytest.raises(membership.AlreadyInOrgError):
        await membership.attach_user_to_org(user, _tenant())


@pytest.mark.asyncio
async def test_lookup_tenant_for_verified_email_ignores_unverified(monkeypatch):
    lookup = AsyncMock()
    monkeypatch.setattr(membership, "lookup_tenant_by_email_domain", lookup)
    user = SimpleNamespace(email="ada@acme.com", email_verified=False)
    assert await membership.lookup_tenant_for_verified_email(user) is None
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_tenant_for_verified_email_uses_verified(monkeypatch):
    acme = _tenant()
    monkeypatch.setattr(membership, "lookup_tenant_by_email_domain", AsyncMock(return_value=acme))
    user = SimpleNamespace(email="ada@acme.com", email_verified=True)
    assert await membership.lookup_tenant_for_verified_email(user) is acme


def test_cannot_delete_system_or_default_org():
    with pytest.raises(membership.DefaultOrgUnavailableError):
        membership.assert_may_delete_tenant(_tenant(is_system=True))
    with pytest.raises(membership.DefaultOrgUnavailableError):
        membership.assert_may_delete_tenant(_tenant(is_default_end_user_org=True))
    membership.assert_may_delete_tenant(_tenant())


@pytest.mark.asyncio
async def test_transfer_user_to_org_blocks_org_admin():
    user = SimpleNamespace(tenant_id=uuid.uuid4(), role="org_admin", is_genesis=False)
    with pytest.raises(membership.AlreadyInOrgError):
        await membership.transfer_user_to_org(user, _tenant())


def test_cannot_disable_default_end_user_org():
    tenant = _tenant(is_default_end_user_org=True)
    with pytest.raises(membership.DefaultOrgUnavailableError):
        membership.assert_may_deactivate_tenant(tenant, making_active=False)


@pytest.mark.asyncio
async def test_consume_invitation_code_increments(monkeypatch):
    code = SimpleNamespace(used_count=1, max_uses=5)
    update = AsyncMock()
    monkeypatch.setattr(membership.invitation_code_dao, "get_active_by_code", AsyncMock(return_value=code))
    monkeypatch.setattr(membership.invitation_code_dao, "update", update)
    await membership.consume_invitation_code("ABCD")
    update.assert_awaited_once()
    assert update.await_args.kwargs["obj_in"]["used_count"] == 2


@pytest.mark.asyncio
async def test_match_user_by_email_reuses_pending(monkeypatch):
    from app.services.sso_service import SSOService

    pending = SimpleNamespace(id=uuid.uuid4(), tenant_id=None, role="member", is_active=False)
    identity = SimpleNamespace(id=uuid.uuid4())
    service = SSOService()
    monkeypatch.setattr("app.services.sso_service.user_dao.get_by_email_and_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.sso_service.identity_dao.get_by_email", AsyncMock(return_value=identity))
    monkeypatch.setattr(
        "app.services.sso_service.user_dao.get_by_identity_id",
        AsyncMock(return_value=[pending]),
    )
    found = await service.match_user_by_email("ada@acme.com", tenant_id=str(uuid.uuid4()))
    assert found is pending


@pytest.mark.asyncio
async def test_create_user_with_identity_copies_tenant_quotas(monkeypatch):
    from app.services.registration_service import RegistrationService

    tenant = _tenant(default_message_limit=80, default_max_agents=4)
    identity = SimpleNamespace(id=uuid.uuid4(), username="ada", email_verified=True, is_platform_admin=False)
    created = {}

    async def _create(*, obj_in):
        created.update(obj_in)
        return SimpleNamespace(id=uuid.uuid4(), identity=identity, avatar_url=None, **obj_in)

    service = RegistrationService()
    monkeypatch.setattr("app.services.registration_service.tenant_dao.get", AsyncMock(return_value=tenant))
    monkeypatch.setattr("app.services.registration_service.user_dao.create", _create)
    monkeypatch.setattr(service, "bind_org_member", AsyncMock())
    monkeypatch.setattr("app.services.registration_service.participant_dao.create_for_user", AsyncMock())
    monkeypatch.setattr("app.services.registration_service.resolve_email_config_async", AsyncMock(return_value=None))

    await service.create_user_with_identity(identity=identity, tenant_id=tenant.id)
    assert created["quota_message_limit"] == 80
    assert created["quota_max_agents"] == 4
