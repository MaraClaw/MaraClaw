"""Genesis-only rules for creating additional platform and org admins."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import admin as admin_api, tenants as tenants_api, users as users_api
from app.services import admin_provisioning as provisioning
from app.services.tenant_provisioning import AdminEmailTakenError


def _user(*, role: str, user_id=None, tenant_id=None, identity=None, is_active=True, email=None, is_genesis=False):
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        role=role,
        tenant_id=tenant_id,
        identity=identity,
        display_name="Admin",
        avatar_url=None,
        is_active=is_active,
        email=email,
        is_genesis=is_genesis,
    )


@pytest.mark.asyncio
async def test_is_genesis_platform_admin_uses_persisted_flag():
    genesis_id = uuid.uuid4()
    assert (
        await provisioning.is_genesis_platform_admin(
            _user(role="platform_admin", user_id=genesis_id, is_genesis=True)
        )
        is True
    )
    assert await provisioning.is_genesis_platform_admin(_user(role="platform_admin")) is False
    assert await provisioning.is_genesis_platform_admin(_user(role="org_admin", is_genesis=True)) is False


@pytest.mark.asyncio
async def test_is_genesis_org_admin_uses_persisted_flag():
    tenant_id = uuid.uuid4()
    genesis_id = uuid.uuid4()
    assert (
        await provisioning.is_genesis_org_admin(
            _user(role="org_admin", user_id=genesis_id, tenant_id=tenant_id, is_genesis=True)
        )
        is True
    )
    assert await provisioning.is_genesis_org_admin(_user(role="org_admin", tenant_id=tenant_id)) is False
    assert await provisioning.is_genesis_org_admin(_user(role="org_admin", tenant_id=None, is_genesis=True)) is False


@pytest.mark.asyncio
async def test_create_platform_admin_rejects_non_genesis(monkeypatch):
    monkeypatch.setattr(admin_api, "is_genesis_platform_admin", AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as exc:
        await admin_api.create_platform_admin(
            admin_api.PlatformAdminCreateRequest(admin_email="pa@example.com", admin_password="temp-password"),
            current_user=_user(role="platform_admin"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_platform_admin_provisions(monkeypatch):
    new_id = uuid.uuid4()
    monkeypatch.setattr(admin_api, "is_genesis_platform_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(
        admin_api,
        "create_additional_platform_admin",
        AsyncMock(
            return_value=SimpleNamespace(
                user=_user(role="platform_admin", user_id=new_id), admin_email="pa@example.com"
            )
        ),
    )
    result = await admin_api.create_platform_admin(
        admin_api.PlatformAdminCreateRequest(admin_email="pa@example.com", admin_password="temp-password"),
        current_user=_user(role="platform_admin"),
    )
    assert result.user_id == new_id
    assert result.must_change_password is True
    assert result.admin_email == "pa@example.com"


@pytest.mark.asyncio
async def test_create_org_admin_rejects_non_genesis(monkeypatch):
    monkeypatch.setattr(users_api, "is_genesis_org_admin", AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as exc:
        await users_api.create_org_admin(
            users_api.OrgAdminCreateRequest(admin_email="oa@example.com", admin_password="temp-password"),
            current_user=_user(role="org_admin", tenant_id=uuid.uuid4()),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_org_admin_provisions(monkeypatch):
    tenant_id = uuid.uuid4()
    new_id = uuid.uuid4()
    monkeypatch.setattr(users_api, "is_genesis_org_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(
        users_api,
        "create_additional_org_admin",
        AsyncMock(
            return_value=SimpleNamespace(
                user=_user(role="org_admin", user_id=new_id, tenant_id=tenant_id),
                admin_email="oa@example.com",
            )
        ),
    )
    result = await users_api.create_org_admin(
        users_api.OrgAdminCreateRequest(admin_email="oa@example.com", admin_password="temp-password"),
        current_user=_user(role="org_admin", tenant_id=tenant_id),
    )
    assert result.user_id == new_id
    assert result.tenant_id == tenant_id
    assert result.must_change_password is True


@pytest.mark.asyncio
async def test_update_role_blocks_non_genesis_from_minting_org_admin():
    tenant_id = uuid.uuid4()
    target = _user(role="member", tenant_id=tenant_id)
    with pytest.raises(provisioning.AdminGuardError) as exc:
        await provisioning.apply_user_role_change(
            actor=_user(role="org_admin", tenant_id=tenant_id),
            target=target,
            new_role="org_admin",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_role_blocks_platform_admin_from_minting_org_admin():
    tenant_id = uuid.uuid4()
    target = _user(role="member", tenant_id=tenant_id)
    with pytest.raises(provisioning.AdminGuardError) as exc:
        await provisioning.apply_user_role_change(
            actor=_user(role="platform_admin", is_genesis=True),
            target=target,
            new_role="org_admin",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_role_genesis_org_admin_can_promote(monkeypatch):
    tenant_id = uuid.uuid4()
    target = _user(role="member", tenant_id=tenant_id)
    promoted = _user(role="org_admin", tenant_id=tenant_id)
    monkeypatch.setattr(provisioning.user_dao, "update", AsyncMock(return_value=promoted))
    monkeypatch.setattr(provisioning, "write_admin_audit", AsyncMock())
    with patch.object(provisioning, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await provisioning.apply_user_role_change(
            actor=_user(role="org_admin", tenant_id=tenant_id, is_genesis=True),
            target=target,
            new_role="org_admin",
        )
    assert result.role == "org_admin"


@pytest.mark.asyncio
async def test_assign_user_rejects_org_admin_role():
    with pytest.raises(HTTPException) as exc:
        await tenants_api.assign_user_to_tenant(
            uuid.uuid4(),
            uuid.uuid4(),
            role="org_admin",
            current_user=_user(role="platform_admin"),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_additional_platform_admin_write_kwargs(monkeypatch):
    identity = SimpleNamespace(id=uuid.uuid4())
    user = _user(role="platform_admin")
    create_identity = AsyncMock(return_value=identity)
    create_user = AsyncMock(return_value=user)
    monkeypatch.setattr(provisioning.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(provisioning.identity_dao, "is_username_taken", AsyncMock(return_value=False))
    monkeypatch.setattr(provisioning, "hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(provisioning.identity_dao, "create_identity", create_identity)
    monkeypatch.setattr(provisioning.user_dao, "create", create_user)
    monkeypatch.setattr(provisioning.participant_dao, "create_for_user", AsyncMock())

    with patch.object(provisioning, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await provisioning.create_additional_platform_admin(
            admin_email="PA@Example.com",
            admin_password="temp-password",
            admin_display_name="Second PA",
        )

    assert result.admin_email == "pa@example.com"
    assert create_identity.await_args.kwargs["is_platform_admin"] is True
    assert create_identity.await_args.kwargs["must_change_password"] is True
    assert create_user.await_args.kwargs["obj_in"]["role"] == "platform_admin"
    assert create_user.await_args.kwargs["obj_in"]["tenant_id"] is None
    assert create_user.await_args.kwargs["obj_in"]["is_genesis"] is False


@pytest.mark.asyncio
async def test_create_additional_org_admin_rejects_taken_email(monkeypatch):
    monkeypatch.setattr(
        provisioning.identity_dao, "get_by_email", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    with pytest.raises(AdminEmailTakenError):
        await provisioning.create_additional_org_admin(
            tenant_id=uuid.uuid4(),
            admin_email="taken@example.com",
            admin_password="temp-password",
        )


@pytest.mark.asyncio
async def test_set_peer_admin_active_rejects_non_genesis_platform(monkeypatch):
    actor = _user(role="platform_admin")
    target = _user(role="platform_admin")
    monkeypatch.setattr(provisioning, "is_genesis_platform_admin", AsyncMock(return_value=False))
    with pytest.raises(provisioning.AdminActivationError) as exc:
        await provisioning.set_peer_admin_active(actor=actor, target=target, is_active=False)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_set_peer_admin_active_rejects_self():
    user = _user(role="platform_admin")
    with pytest.raises(provisioning.AdminActivationError) as exc:
        await provisioning.set_peer_admin_active(actor=user, target=user, is_active=False)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_set_peer_admin_active_deactivates_other_platform_admin(monkeypatch):
    genesis = _user(role="platform_admin")
    target = _user(role="platform_admin", is_active=True)

    async def _is_genesis(user):
        return user.id == genesis.id

    monkeypatch.setattr(provisioning, "is_genesis_platform_admin", _is_genesis)
    monkeypatch.setattr(
        provisioning.user_dao,
        "deactivate_unless_last_active",
        AsyncMock(return_value=SimpleNamespace(**{**target.__dict__, "is_active": False})),
    )
    monkeypatch.setattr(provisioning, "write_admin_audit", AsyncMock())

    with patch.object(provisioning, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        updated = await provisioning.set_peer_admin_active(actor=genesis, target=target, is_active=False)
    assert updated.is_active is False
    provisioning.write_admin_audit.assert_awaited_once()
    assert provisioning.write_admin_audit.await_args.kwargs["action"] == "user_deactivate"
    assert "is_active" in provisioning.write_admin_audit.await_args.kwargs["changes"]


@pytest.mark.asyncio
async def test_set_peer_admin_active_rejects_genesis_org_target(monkeypatch):
    tenant_id = uuid.uuid4()
    genesis = _user(role="org_admin", tenant_id=tenant_id)
    target = _user(role="org_admin", tenant_id=tenant_id)

    async def _is_genesis(user):
        return True

    monkeypatch.setattr(provisioning, "is_genesis_org_admin", _is_genesis)
    with pytest.raises(provisioning.AdminActivationError) as exc:
        await provisioning.set_peer_admin_active(actor=genesis, target=target, is_active=False)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_write_admin_audit_persists_actor_and_changes(monkeypatch):
    from app.services import admin_audit

    create = AsyncMock()
    monkeypatch.setattr(admin_audit.admin_audit_log_dao, "create", create)
    actor = _user(role="platform_admin", email="genesis@example.com")
    target_id = uuid.uuid4()
    await admin_audit.write_admin_audit(
        actor=actor,
        action="user_deactivate",
        target_type="user",
        target_id=target_id,
        changes={"is_active": admin_audit.field_change(True, False)},
        ip_address="127.0.0.1",
    )
    payload = create.await_args.kwargs["obj_in"]
    assert payload["actor_id"] == actor.id
    assert payload["actor_role"] == "platform_admin"
    assert payload["actor_email"] == "genesis@example.com"
    assert payload["action"] == "user_deactivate"
    assert payload["changes"]["is_active"] == {"before": True, "after": False}
    assert payload["ip_address"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_apply_user_role_change_refuses_genesis_demotion():
    genesis = _user(role="platform_admin", is_genesis=True)
    with pytest.raises(provisioning.AdminGuardError) as exc:
        await provisioning.apply_user_role_change(
            actor=_user(role="platform_admin", is_genesis=True),
            target=genesis,
            new_role="member",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_apply_user_role_change_refuses_last_active_demotion(monkeypatch):
    target = _user(role="platform_admin", is_active=True)
    monkeypatch.setattr(provisioning.user_dao, "count_active_by_role", AsyncMock(return_value=1))
    with pytest.raises(provisioning.AdminGuardError) as exc:
        await provisioning.apply_user_role_change(
            actor=_user(role="platform_admin", is_genesis=True),
            target=target,
            new_role="member",
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_apply_user_assignment_refuses_genesis_and_clears_platform_flag(monkeypatch):
    tenant_id = uuid.uuid4()
    genesis = _user(role="platform_admin", is_genesis=True)
    with pytest.raises(provisioning.AdminGuardError) as exc:
        await provisioning.apply_user_assignment(
            actor=_user(role="platform_admin", is_genesis=True),
            target=genesis,
            tenant_id=tenant_id,
            role="member",
        )
    assert exc.value.status_code == 403

    identity = SimpleNamespace(id=uuid.uuid4(), is_platform_admin=True)
    peer = _user(role="platform_admin", identity=identity, is_active=True)
    monkeypatch.setattr(provisioning.user_dao, "count_active_by_role", AsyncMock(return_value=2))
    monkeypatch.setattr(provisioning.identity_dao, "update", AsyncMock())
    monkeypatch.setattr(
        provisioning.user_dao,
        "update",
        AsyncMock(side_effect=lambda db_obj, obj_in: SimpleNamespace(**{**db_obj.__dict__, **obj_in})),
    )
    monkeypatch.setattr(provisioning, "write_admin_audit", AsyncMock())
    with patch.object(provisioning, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        updated = await provisioning.apply_user_assignment(
            actor=_user(role="platform_admin", is_genesis=True),
            target=peer,
            tenant_id=tenant_id,
            role="member",
        )
    assert updated.role == "member"
    provisioning.identity_dao.update.assert_awaited_once()
    assert provisioning.identity_dao.update.await_args.kwargs["obj_in"]["is_platform_admin"] is False


def test_join_refuses_genesis_and_platform_admin_rewrite():
    with pytest.raises(provisioning.AdminGuardError) as exc:
        provisioning.assert_join_may_rewrite_membership(_user(role="platform_admin", is_genesis=True))
    assert exc.value.status_code == 403
    with pytest.raises(provisioning.AdminGuardError) as exc:
        provisioning.assert_join_may_rewrite_membership(_user(role="platform_admin"))
    assert exc.value.status_code == 403
    provisioning.assert_join_may_rewrite_membership(_user(role="member", tenant_id=None))
