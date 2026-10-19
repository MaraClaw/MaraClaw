import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import organization
from app.dao.identity_dao import identity_dao
from app.dao.user_dao import user_dao
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.schemas import UserUpdate
from app.services.registration_service import registration_service


def _user(tenant_id: uuid.UUID | None, role: str = "member") -> UserRecord:
    identity = IdentityRecord(id=uuid.uuid4(), email="before@example.com", username="before", phone="100")
    return UserRecord(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        tenant_id=tenant_id,
        role=role,
        display_name="Before",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _patch_store(monkeypatch, target):
    async def update(*, db_obj, obj_in):
        for field, value in obj_in.items():
            setattr(db_obj, field, value)
        return db_obj

    user_write = AsyncMock(side_effect=update)
    identity_write = AsyncMock(side_effect=update)
    sync = AsyncMock()
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(return_value=target))
    monkeypatch.setattr(user_dao, "update", user_write)
    monkeypatch.setattr(identity_dao, "get", AsyncMock(return_value=target.identity))
    monkeypatch.setattr(identity_dao, "update", identity_write)
    monkeypatch.setattr(identity_dao, "is_email_taken_in_tenant", AsyncMock(return_value=False))
    monkeypatch.setattr(identity_dao, "is_phone_taken_in_tenant", AsyncMock(return_value=False))
    monkeypatch.setattr(registration_service, "sync_org_member_contact_from_user", sync)
    return user_write, identity_write, sync


@pytest.mark.parametrize("role", ["org_admin", "member", "agent_admin"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_list_uses_own_tenant_when_filter_is_omitted_or_own(monkeypatch, role, explicit):
    actor = _user(uuid.uuid4(), role)
    target = _user(actor.tenant_id)
    listing = AsyncMock(return_value=[target])
    monkeypatch.setattr(user_dao, "list_active_for_tenant", listing)

    result = await organization.list_users(actor.tenant_id if explicit else None, actor)

    assert [user.id for user in result] == [target.id]
    listing.assert_awaited_once_with(actor.tenant_id, include_identity=True, order_by_display_name=True)


async def test_list_refuses_foreign_org_admin_filter_instead_of_ignoring_it(monkeypatch):
    actor = _user(uuid.uuid4(), "org_admin")
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(user_dao, "list_active_for_tenant", listing)

    with pytest.raises(HTTPException) as error:
        await organization.list_users(uuid.uuid4(), actor)

    assert error.value.status_code == 403
    listing.assert_not_awaited()


@pytest.mark.parametrize("role", ["member", "agent_admin", "platform_admin"])
async def test_list_preserves_other_roles_foreign_filter_behavior(monkeypatch, role):
    actor = _user(uuid.uuid4(), role)
    foreign = uuid.uuid4()
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(user_dao, "list_active_for_tenant", listing)

    assert await organization.list_users(foreign, actor) == []

    expected = foreign if role == "platform_admin" else actor.tenant_id
    listing.assert_awaited_once_with(expected, include_identity=True, order_by_display_name=True)


@pytest.mark.parametrize("tenantless", [False, True])
async def test_patch_hides_foreign_targets_from_org_admin(monkeypatch, tenantless):
    actor = _user(None if tenantless else uuid.uuid4(), "org_admin")
    target = _user(uuid.uuid4())
    writes = _patch_store(monkeypatch, target)

    with pytest.raises(HTTPException) as error:
        await organization.admin_update_user(target.id, UserUpdate(display_name="Changed"), actor)

    assert error.value.status_code == 404
    for write in writes:
        write.assert_not_awaited()


@pytest.mark.parametrize("protection", ["platform_role", "platform_identity", "genesis", "genesis_self"])
@pytest.mark.parametrize("field", ["email", "username", "primary_mobile"])
async def test_patch_rejects_entire_request_for_protected_identities(monkeypatch, protection, field):
    actor = _user(uuid.uuid4(), "org_admin")
    target = actor if protection == "genesis_self" else _user(actor.tenant_id)
    target.is_genesis = protection in ("genesis", "genesis_self")
    if protection == "platform_role":
        target.role = "platform_admin"
    if protection == "platform_identity":
        assert target.identity is not None
        target.identity.is_platform_admin = True
    writes = _patch_store(monkeypatch, target)
    value = "after@example.com" if field == "email" else "after"
    data = UserUpdate.model_validate({field: value, "display_name": "Changed"})

    with pytest.raises(HTTPException) as error:
        await organization.admin_update_user(target.id, data, actor)

    assert error.value.status_code == 403
    assert target.display_name == "Before"
    for write in writes:
        write.assert_not_awaited()


async def test_patch_rejects_even_profile_only_changes_to_platform_admin(monkeypatch):
    actor = _user(uuid.uuid4(), "org_admin")
    target = _user(actor.tenant_id, "platform_admin")
    writes = _patch_store(monkeypatch, target)

    with pytest.raises(HTTPException) as error:
        await organization.admin_update_user(target.id, UserUpdate(display_name="Changed"), actor)

    assert error.value.status_code == 403
    for write in writes:
        write.assert_not_awaited()


async def test_patch_allows_genesis_profile_without_identity_changes(monkeypatch):
    actor = _user(uuid.uuid4(), "org_admin")
    target = _user(actor.tenant_id, "org_admin")
    target.is_genesis = True
    _, identity_write, sync = _patch_store(monkeypatch, target)

    result = await organization.admin_update_user(target.id, UserUpdate(display_name="Changed"), actor)

    assert result.display_name == "Changed"
    identity_write.assert_not_awaited()
    sync.assert_not_awaited()


@pytest.mark.parametrize("platform", [False, True])
async def test_patch_allows_normal_identity_fields_and_preserves_contact_checks(monkeypatch, platform):
    actor = _user(uuid.uuid4(), "platform_admin" if platform else "org_admin")
    target = _user(uuid.uuid4() if platform else actor.tenant_id)
    _, identity_write, sync = _patch_store(monkeypatch, target)
    email_check = AsyncMock(return_value=False)
    phone_check = AsyncMock(return_value=False)
    monkeypatch.setattr(identity_dao, "is_email_taken_in_tenant", email_check)
    monkeypatch.setattr(identity_dao, "is_phone_taken_in_tenant", phone_check)
    data = UserUpdate.model_validate({"email": "after@example.com", "username": "after", "primary_mobile": "200"})

    result = await organization.admin_update_user(target.id, data, actor)

    assert (result.email, result.username, result.primary_mobile) == ("after@example.com", "after", "200")
    identity_write.assert_awaited_once()
    email_check.assert_awaited_once_with("after@example.com", target.tenant_id, exclude_user_id=target.id)
    phone_check.assert_awaited_once_with("200", target.tenant_id, exclude_user_id=target.id)
    sync.assert_awaited_once_with(target, sync_email=True, sync_phone=True)


@pytest.mark.parametrize("field", ["email", "primary_mobile"])
async def test_patch_preserves_contact_uniqueness_conflicts(monkeypatch, field):
    actor = _user(uuid.uuid4(), "org_admin")
    target = _user(actor.tenant_id)
    writes = _patch_store(monkeypatch, target)
    check = "is_email_taken_in_tenant" if field == "email" else "is_phone_taken_in_tenant"
    monkeypatch.setattr(identity_dao, check, AsyncMock(return_value=True))
    value = "taken@example.com" if field == "email" else "200"

    with pytest.raises(HTTPException) as error:
        await organization.admin_update_user(target.id, UserUpdate.model_validate({field: value}), actor)

    assert error.value.status_code == 409
    for write in writes:
        write.assert_not_awaited()


@pytest.mark.parametrize("identity_admin", [False, True])
async def test_platform_admin_can_edit_foreign_genesis_platform_identity(monkeypatch, identity_admin):
    actor = _user(uuid.uuid4(), "org_admin" if identity_admin else "platform_admin")
    assert actor.identity is not None
    actor.identity.is_platform_admin = identity_admin
    target = _user(uuid.uuid4(), "platform_admin")
    target.is_genesis = True
    _patch_store(monkeypatch, target)

    result = await organization.admin_update_user(
        target.id, UserUpdate.model_validate({"email": "after@example.com"}), actor
    )

    assert result.email == "after@example.com"


async def test_patch_preserves_missing_user_response(monkeypatch):
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as error:
        await organization.admin_update_user(uuid.uuid4(), UserUpdate(), _user(uuid.uuid4(), "org_admin"))

    assert error.value.status_code == 404
