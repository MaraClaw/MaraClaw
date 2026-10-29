"""Email-domain lookup and one-org placement."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.domain.users import UserRole
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services import org_membership as membership


def _tenant(
    *,
    name: str = "Acme",
    slug: str = "acme",
    default_message_limit: int = 50,
    default_max_agents: int = 2,
    is_default_end_user_org: bool = False,
    is_system: bool = False,
) -> TenantRecord:
    return TenantRecord(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        default_message_limit=default_message_limit,
        default_max_agents=default_max_agents,
        is_default_end_user_org=is_default_end_user_org,
        is_system=is_system,
    )


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
async def test_place_new_registration_claimed_domain_waits_for_initial_admission(monkeypatch):
    acme = _tenant()
    kabers = _tenant(name="KABERs", slug="kabers", is_default_end_user_org=True)
    claim = SimpleNamespace(tenant_id=acme.id)
    monkeypatch.setattr(membership.tenant_email_domain_dao, "get_by_domain", AsyncMock(return_value=claim))
    monkeypatch.setattr(membership.tenant_dao, "get", AsyncMock(return_value=acme))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=kabers))

    placed = await membership.place_new_registration(email="ada@acme.com")
    assert placed.needs_org_confirm is True
    assert placed.tenant_id is None
    assert placed.suggested is acme


@pytest.mark.asyncio
async def test_place_new_registration_autojoins_kabers(monkeypatch):
    kabers = _tenant(name="KABERs", slug="kabers", is_default_end_user_org=True)
    monkeypatch.setattr(membership.tenant_email_domain_dao, "get_by_domain", AsyncMock(return_value=None))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=kabers))

    placed = await membership.place_new_registration(email="bob@gmail.com")
    assert placed.needs_org_confirm is False
    assert placed.tenant_id == kabers.id


@pytest.mark.asyncio
async def test_place_new_registration_invite_stays_in_kabers(monkeypatch):
    invited = _tenant()
    kabers = _tenant(name="KABERs", slug="kabers", is_default_end_user_org=True)
    code = SimpleNamespace(tenant_id=invited.id, used_count=0, max_uses=5)
    monkeypatch.setattr(membership.invitation_code_dao, "get_active_by_code", AsyncMock(return_value=code))
    monkeypatch.setattr(membership.tenant_dao, "get", AsyncMock(return_value=invited))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=kabers))

    placed = await membership.place_new_registration(email="x@y.com", invitation_code="ABCD")
    assert placed.needs_org_confirm is False
    assert placed.tenant_id == kabers.id


@pytest.mark.asyncio
async def test_invalid_invite_is_invitation_error_not_503(monkeypatch):
    kabers = _tenant(name="KABERs", slug="kabers", is_default_end_user_org=True)
    monkeypatch.setattr(membership.invitation_code_dao, "get_active_by_code", AsyncMock(return_value=None))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=kabers))
    with pytest.raises(membership.InvitationError):
        await membership.place_new_registration(email="x@y.com", invitation_code="NOPE")


@pytest.mark.asyncio
async def test_exhausted_invite_is_invitation_error(monkeypatch):
    kabers = _tenant(name="KABERs", slug="kabers", is_default_end_user_org=True)
    code = SimpleNamespace(tenant_id=uuid.uuid4(), used_count=5, max_uses=5)
    monkeypatch.setattr(membership.invitation_code_dao, "get_active_by_code", AsyncMock(return_value=code))
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=kabers))
    with pytest.raises(membership.InvitationError):
        await membership.place_new_registration(invitation_code="USED")


def _slug_lookup(monkeypatch, **by_slug: TenantRecord) -> AsyncMock:
    lookup = AsyncMock(side_effect=lambda slug: by_slug.get(slug))
    monkeypatch.setattr(membership.tenant_dao, "get_by_slug", lookup)
    return lookup


@pytest.mark.asyncio
async def test_get_fallback_org_prefers_the_default_end_user_org(monkeypatch):
    default = _tenant(name="Acme", slug="acme", is_default_end_user_org=True)
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=default))
    lookup = _slug_lookup(monkeypatch, kabers=_tenant(name="KABERs", slug="kabers"))

    assert await membership.get_fallback_org() is default
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_fallback_org_resolves_kabers_slug_without_a_default_flag(monkeypatch):
    kabers = _tenant(name="KABERs", slug="kabers", is_system=True)
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=None))
    lookup = _slug_lookup(monkeypatch, kabers=kabers)

    assert await membership.get_fallback_org() is kabers
    lookup.assert_awaited_once_with("kabers")


@pytest.mark.asyncio
async def test_get_fallback_org_rejects_missing_or_disabled_org(monkeypatch):
    monkeypatch.setattr(membership.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=None))
    disabled = _tenant(name="KABERs", slug="kabers", is_system=True)
    disabled.is_active = False
    _slug_lookup(monkeypatch, kabers=disabled)
    with pytest.raises(membership.DefaultOrgUnavailableError):
        await membership.get_fallback_org()

    _slug_lookup(monkeypatch)
    with pytest.raises(membership.DefaultOrgUnavailableError):
        await membership.get_fallback_org()


@pytest.mark.asyncio
async def test_attach_user_to_org_refuses_second_membership():
    user = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    with pytest.raises(membership.AlreadyInOrgError):
        await membership.attach_user_to_org(user, _tenant())


@pytest.mark.asyncio
async def test_transfer_user_to_org_moves_member(monkeypatch):
    source = uuid.uuid4()
    dest = _tenant()
    user = UserRecord(id=uuid.uuid4(), tenant_id=source)
    updated = replace(user, tenant_id=dest.id)
    unbind = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield None

    monkeypatch.setattr(membership.user_dao, "update", AsyncMock(return_value=updated))
    monkeypatch.setattr(
        membership.tenant_dao,
        "get",
        AsyncMock(return_value=_tenant(is_default_end_user_org=True)),
    )
    monkeypatch.setattr(membership.org_member_dao, "unbind_user_from_tenant", unbind)
    monkeypatch.setattr("app.services.registration_service.registration_service.bind_org_member", AsyncMock())
    monkeypatch.setattr(membership, "connection_ctx", _ctx)

    result = await membership.transfer_user_to_org(user, dest)
    assert result.tenant_id == dest.id
    unbind.assert_awaited_once_with(user.id, source)


@pytest.mark.asyncio
async def test_attach_user_to_org_blocks_platform_admin():
    user = UserRecord(id=uuid.uuid4(), role=UserRole.PLATFORM_ADMIN)
    with pytest.raises(membership.AlreadyInOrgError):
        await membership.attach_user_to_org(user, _tenant())


@pytest.mark.asyncio
async def test_lookup_tenant_for_verified_email_ignores_unverified(monkeypatch):
    lookup = AsyncMock()
    monkeypatch.setattr(membership, "lookup_tenant_by_email_domain", lookup)
    user = UserRecord(
        id=uuid.uuid4(), identity=IdentityRecord(id=uuid.uuid4(), email="ada@acme.com", email_verified=False)
    )
    assert await membership.lookup_tenant_for_verified_email(user) is None
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_tenant_for_verified_email_uses_verified(monkeypatch):
    acme = _tenant()
    monkeypatch.setattr(membership, "lookup_tenant_by_email_domain", AsyncMock(return_value=acme))
    user = UserRecord(
        id=uuid.uuid4(), identity=IdentityRecord(id=uuid.uuid4(), email="ada@acme.com", email_verified=True)
    )
    assert await membership.lookup_tenant_for_verified_email(user) is acme


def test_cannot_delete_system_or_default_org():
    with pytest.raises(membership.DefaultOrgUnavailableError):
        membership.assert_may_delete_tenant(_tenant(is_system=True))
    with pytest.raises(membership.DefaultOrgUnavailableError):
        membership.assert_may_delete_tenant(_tenant(is_default_end_user_org=True))
    membership.assert_may_delete_tenant(_tenant())


@pytest.mark.asyncio
async def test_transfer_user_to_org_blocks_org_admin():
    user = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=UserRole.ORG_ADMIN)
    with pytest.raises(membership.AlreadyInOrgError):
        await membership.transfer_user_to_org(user, _tenant())


def test_cannot_disable_default_end_user_org():
    tenant = _tenant(is_default_end_user_org=True)
    with pytest.raises(membership.DefaultOrgUnavailableError):
        membership.assert_may_deactivate_tenant(tenant, making_active=False)


def test_cannot_disable_system_org():
    tenant = _tenant(is_system=True)
    with pytest.raises(membership.DefaultOrgUnavailableError):
        membership.assert_may_deactivate_tenant(tenant, making_active=False)


@pytest.mark.asyncio
async def test_consume_invitation_code_spends_one_use_atomically_for_the_recipient(monkeypatch):
    consume = AsyncMock(return_value=True)
    monkeypatch.setattr(membership.invitation_code_dao, "consume", consume)
    user_id = uuid.uuid4()

    await membership.consume_invitation_code("ABCD", user_id=user_id, email=" Bob@Example.COM ")

    consume.assert_awaited_once_with("ABCD", user_id=user_id, email="bob@example.com")


@pytest.mark.asyncio
async def test_consume_invitation_code_rejects_a_lost_race(monkeypatch):
    monkeypatch.setattr(membership.invitation_code_dao, "consume", AsyncMock(return_value=False))

    with pytest.raises(membership.InvitationError):
        await membership.consume_invitation_code("ABCD")


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
    identity = IdentityRecord(id=uuid.uuid4(), email="ada@example.com", email_verified=True)
    created: dict[str, object] = {}

    async def _create(*, obj_in: dict[str, object]) -> UserRecord:
        created.update(obj_in)
        return replace(UserRecord(id=uuid.uuid4(), identity=identity), **obj_in)

    service = RegistrationService()
    monkeypatch.setattr("app.services.registration_service.tenant_dao.get", AsyncMock(return_value=tenant))
    monkeypatch.setattr("app.services.registration_service.user_dao.create", _create)
    monkeypatch.setattr(service, "bind_org_member", AsyncMock())
    monkeypatch.setattr("app.services.registration_service.participant_dao.create_for_user", AsyncMock())
    monkeypatch.setattr("app.services.registration_service.resolve_email_config_async", AsyncMock(return_value=None))

    await service.create_user_with_identity(identity=identity, tenant_id=tenant.id)
    assert created["quota_message_limit"] == 80
    assert created["quota_max_agents"] == 4
