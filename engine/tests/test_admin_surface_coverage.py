"""Raise coverage on the 90% admin / tenant / auth gate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import (
    admin as admin_api,
    auth as auth_api,
    enterprise as enterprise_api,
    tenants as tenants_api,
    users as users_api,
)
from app.records.audit import AdminAuditLogRecord
from app.records.tenant import TenantRecord
from app.services import admin_provisioning as provisioning
from app.services.admin_audit import write_admin_audit
from app.services.tenant_provisioning import AdminEmailTakenError

_NOW = datetime.now(UTC)


def _user(
    *,
    role="platform_admin",
    user_id=None,
    tenant_id=None,
    is_active=True,
    email="a@b.com",
    is_genesis=False,
    email_verified=True,
):
    uid = user_id or uuid.uuid4()
    return SimpleNamespace(
        id=uid,
        identity_id=uid,
        role=role,
        tenant_id=tenant_id,
        identity=None,
        display_name="Admin",
        avatar_url=None,
        is_active=is_active,
        is_genesis=is_genesis,
        email=email,
        email_verified=email_verified,
        username="admin",
        created_at=_NOW,
        quota_message_limit=50,
        quota_message_period="permanent",
        quota_messages_used=0,
        quota_max_agents=2,
        quota_agent_ttl_hours=0,
        registration_source="web",
    )


def _tenant(**kwargs):
    defaults = {
        "id": uuid.uuid4(),
        "name": "Acme",
        "slug": "acme-abc",
        "im_provider": "web_only",
        "timezone": "UTC",
        "country_region": "001",
        "is_active": True,
        "sso_enabled": False,
        "sso_domain": None,
        "a2a_async_enabled": True,
        "default_model_id": None,
        "created_at": _NOW,
        "default_message_limit": 50,
        "default_message_period": "permanent",
        "default_max_agents": 2,
        "default_agent_ttl_hours": 0,
        "im_config": None,
    }
    defaults.update(kwargs)
    return TenantRecord(**{k: defaults[k] for k in TenantRecord.__dataclass_fields__ if k in defaults})


@pytest.mark.asyncio
async def test_get_client_ip_none_and_host():
    req = MagicMock(spec=Request)
    req.client = None
    assert await admin_api.get_client_ip(req) is None
    req.client = SimpleNamespace(host="10.0.0.2")
    assert await admin_api.get_client_ip(req) == "10.0.0.2"
    assert await users_api.get_client_ip(req) == "10.0.0.2"


@pytest.mark.asyncio
async def test_list_companies(monkeypatch):
    tenant = _tenant()
    monkeypatch.setattr(admin_api.tenant_dao, "list_ordered_by_created_at", AsyncMock(return_value=[tenant]))
    async def count_for_tenant(_tid, *, is_active=None):
        if is_active is True:
            return 2
        return 3

    monkeypatch.setattr(admin_api.user_dao, "count_for_tenant", AsyncMock(side_effect=count_for_tenant))
    monkeypatch.setattr(admin_api.agent_dao, "count_for_tenant", AsyncMock(return_value=2))
    monkeypatch.setattr(admin_api.agent_dao, "sum_tokens_for_tenant", AsyncMock(return_value=(10, 1)))
    monkeypatch.setattr(admin_api.user_dao, "first_org_admin_email", AsyncMock(return_value="oa@acme.com"))
    result = await admin_api.list_companies(current_user=_user())
    assert len(result) == 1
    assert result[0].org_admin_email == "oa@acme.com"
    assert result[0].user_count == 3
    assert result[0].active_user_count == 2
    assert result[0].inactive_user_count == 1
    assert result[0].agent_count == 2
    assert result[0].can_disable is True


@pytest.mark.asyncio
async def test_list_companies_uses_name_search(monkeypatch):
    tenant = _tenant()
    search = AsyncMock(return_value=[tenant])
    monkeypatch.setattr(admin_api.tenant_dao, "search_by_name", search)
    monkeypatch.setattr(admin_api.user_dao, "count_for_tenant", AsyncMock(return_value=1))
    monkeypatch.setattr(admin_api.agent_dao, "count_for_tenant", AsyncMock(return_value=0))
    monkeypatch.setattr(admin_api.agent_dao, "sum_tokens_for_tenant", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(admin_api.user_dao, "first_org_admin_email", AsyncMock(return_value=None))
    result = await admin_api.list_companies(q="  mara  ", current_user=_user())
    assert len(result) == 1
    search.assert_awaited_once_with("mara")


@pytest.mark.asyncio
async def test_toggle_company_pauses_when_disabling(monkeypatch):
    tenant = _tenant(is_active=True)
    monkeypatch.setattr(admin_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    set_active = AsyncMock(return_value=tenant)
    monkeypatch.setattr(admin_api, "set_tenant_active", set_active)
    monkeypatch.setattr(admin_api, "write_admin_audit", AsyncMock())
    result = await admin_api.toggle_company(tenant.id, _user())
    assert result["is_active"] is False
    set_active.assert_awaited_once()
    assert set_active.await_args.kwargs["is_active"] is False


@pytest.mark.asyncio
async def test_toggle_company_rejects_system_and_default_orgs(monkeypatch):
    from app.services.org_membership import DefaultOrgUnavailableError

    system = _tenant(is_system=True)
    monkeypatch.setattr(admin_api.tenant_dao, "get", AsyncMock(return_value=system))
    monkeypatch.setattr(
        admin_api,
        "set_tenant_active",
        AsyncMock(side_effect=DefaultOrgUnavailableError("Cannot disable a system organization")),
    )
    with pytest.raises(HTTPException) as exc:
        await admin_api.toggle_company(system.id, _user())
    assert exc.value.status_code == 400

    default = _tenant(is_default_end_user_org=True)
    monkeypatch.setattr(admin_api.tenant_dao, "get", AsyncMock(return_value=default))
    monkeypatch.setattr(
        admin_api,
        "set_tenant_active",
        AsyncMock(side_effect=DefaultOrgUnavailableError("Cannot disable the default end-user organization")),
    )
    with pytest.raises(HTTPException) as exc:
        await admin_api.toggle_company(default.id, _user())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_toggle_company_not_found(monkeypatch):
    monkeypatch.setattr(admin_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await admin_api.toggle_company(uuid.uuid4(), _user())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_and_toggle_platform_admins(monkeypatch):
    genesis = _user(role="platform_admin", is_genesis=True)
    other = _user(role="platform_admin", is_active=True)
    monkeypatch.setattr(admin_api.user_dao, "list_by_role", AsyncMock(return_value=[genesis, other]))
    listed = await admin_api.list_platform_admins(genesis)
    assert listed[0].is_genesis is True
    assert listed[1].is_genesis is False

    monkeypatch.setattr(admin_api.user_dao, "get_with_identity", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await admin_api.set_platform_admin_active(other.id, admin_api.AdminActiveUpdate(is_active=False), genesis)
    assert exc.value.status_code == 404

    monkeypatch.setattr(admin_api.user_dao, "get_with_identity", AsyncMock(return_value=other))
    monkeypatch.setattr(admin_api, "set_peer_admin_active", AsyncMock(return_value=other))
    out = await admin_api.set_platform_admin_active(
        other.id, admin_api.AdminActiveUpdate(is_active=False), genesis, client_ip="1.1.1.1"
    )
    assert out.id == other.id

    monkeypatch.setattr(
        admin_api,
        "set_peer_admin_active",
        AsyncMock(side_effect=provisioning.AdminActivationError(403, "nope")),
    )
    with pytest.raises(HTTPException) as exc:
        await admin_api.set_platform_admin_active(other.id, admin_api.AdminActiveUpdate(is_active=False), genesis)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_admin_audit_logs(monkeypatch):
    rec = AdminAuditLogRecord(
        id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        actor_role="platform_admin",
        actor_email="a@b.com",
        action="tenant_create",
        target_type="tenant",
        target_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        changes={"name": {"before": None, "after": "Acme"}},
        details={},
        created_at=_NOW,
    )
    monkeypatch.setattr(admin_api.admin_audit_log_dao, "list_recent", AsyncMock(return_value=[rec]))
    result = await admin_api.list_admin_audit_logs(current_user=_user())
    assert result[0].action == "tenant_create"


@pytest.mark.asyncio
async def test_platform_settings_get_and_update(monkeypatch):
    monkeypatch.setattr(admin_api.system_setting_dao, "is_flag_enabled", AsyncMock(return_value=False))
    settings = await admin_api.get_platform_settings(_user())
    assert settings.allow_self_create_company is False

    set_flag = AsyncMock()
    monkeypatch.setattr(admin_api.system_setting_dao, "set_flag", set_flag)
    monkeypatch.setattr(admin_api.system_setting_dao, "is_flag_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_api, "write_admin_audit", AsyncMock())
    updated = await admin_api.update_platform_settings(
        admin_api.PlatformSettingsUpdate(invitation_code_enabled=True), _user()
    )
    set_flag.assert_awaited()
    assert updated.invitation_code_enabled is True


@pytest.mark.asyncio
async def test_create_platform_admin_409(monkeypatch):
    monkeypatch.setattr(admin_api, "is_genesis_platform_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(
        admin_api,
        "create_additional_platform_admin",
        AsyncMock(side_effect=AdminEmailTakenError("x@y.com")),
    )
    with pytest.raises(HTTPException) as exc:
        await admin_api.create_platform_admin(
            admin_api.PlatformAdminCreateRequest(admin_email="x@y.com", admin_password="secret1"),
            current_user=_user(),
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_users_list_quota_and_org_admin_surface(monkeypatch):
    tenant_id = uuid.uuid4()
    org = _user(role="org_admin", tenant_id=tenant_id)
    member = _user(role="member", tenant_id=tenant_id)
    monkeypatch.setattr(users_api.user_dao, "list_for_tenant_ordered", AsyncMock(return_value=[member]))
    monkeypatch.setattr(users_api.agent_dao, "count_active_for_creator", AsyncMock(return_value=1))
    listed = await users_api.list_users(current_user=org)
    assert listed[0].agents_count == 1

    with pytest.raises(HTTPException):
        await users_api.list_users(current_user=_user(role="member"))

    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=member))
    monkeypatch.setattr(users_api.user_dao, "update", AsyncMock(return_value=member))
    out = await users_api.update_user_quota(member.id, users_api.UserQuotaUpdate(quota_message_limit=9), org)
    assert out.id == member.id

    with pytest.raises(HTTPException):
        await users_api.update_user_quota(member.id, users_api.UserQuotaUpdate(), _user(role="member"))

    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await users_api.update_user_quota(uuid.uuid4(), users_api.UserQuotaUpdate(), org)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_set_user_active_org_admin(monkeypatch):
    tenant_id = uuid.uuid4()
    org = _user(role="org_admin", tenant_id=tenant_id)
    member = _user(role="member", tenant_id=tenant_id, is_active=True)
    deactivated = _user(role="member", tenant_id=tenant_id, user_id=member.id, is_active=False)
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(side_effect=[member, deactivated]))
    monkeypatch.setattr(users_api, "set_end_user_active", AsyncMock(return_value=deactivated))
    monkeypatch.setattr(users_api.agent_dao, "count_active_for_creator", AsyncMock(return_value=0))
    out = await users_api.set_user_active(member.id, users_api.UserActiveUpdate(is_active=False), org)
    assert out.is_active is False

    with pytest.raises(HTTPException):
        await users_api.set_user_active(member.id, users_api.UserActiveUpdate(is_active=False), _user(role="member"))

    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as missing:
        await users_api.set_user_active(uuid.uuid4(), users_api.UserActiveUpdate(is_active=False), org)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_set_user_active_org_admin_cannot_change_other_company(monkeypatch):
    org = _user(role="org_admin", tenant_id=uuid.uuid4())
    outsider = _user(role="member", tenant_id=uuid.uuid4(), is_active=True)
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=outsider))
    activate = AsyncMock()
    monkeypatch.setattr(users_api, "set_end_user_active", activate)
    with pytest.raises(HTTPException) as exc:
        await users_api.set_user_active(outsider.id, users_api.UserActiveUpdate(is_active=False), org)
    assert exc.value.status_code == 403
    activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_users_org_admin_ignores_foreign_tenant_id(monkeypatch):
    tenant_id = uuid.uuid4()
    org = _user(role="org_admin", tenant_id=tenant_id)
    member = _user(role="member", tenant_id=tenant_id)
    listed = AsyncMock(return_value=[member])
    monkeypatch.setattr(users_api.user_dao, "list_for_tenant_ordered", listed)
    monkeypatch.setattr(users_api.agent_dao, "count_active_for_creator", AsyncMock(return_value=0))
    rows = await users_api.list_users(tenant_id=str(uuid.uuid4()), current_user=org)
    assert len(rows) == 1
    listed.assert_awaited_once()
    assert listed.await_args.args[0] == tenant_id


@pytest.mark.asyncio
async def test_org_admin_list_active_and_audit(monkeypatch):
    tenant_id = uuid.uuid4()
    genesis = _user(role="org_admin", tenant_id=tenant_id, is_genesis=True)
    other = _user(role="org_admin", tenant_id=tenant_id)
    monkeypatch.setattr(users_api.user_dao, "list_org_admins_for_tenant", AsyncMock(return_value=[genesis, other]))
    listed = await users_api.list_org_admins(genesis)
    assert listed[0].is_genesis is True

    with pytest.raises(HTTPException):
        await users_api.list_org_admins(_user(role="org_admin", tenant_id=None))

    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await users_api.set_org_admin_active(other.id, users_api.AdminActiveUpdate(is_active=False), genesis)
    assert exc.value.status_code == 404

    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=other))
    monkeypatch.setattr(users_api, "set_peer_admin_active", AsyncMock(return_value=other))
    out = await users_api.set_org_admin_active(other.id, users_api.AdminActiveUpdate(is_active=True), genesis)
    assert out.id == other.id

    rec = AdminAuditLogRecord(
        id=uuid.uuid4(),
        actor_id=genesis.id,
        actor_role="org_admin",
        actor_email="oa@acme.com",
        action="org_admin_create",
        target_type="user",
        target_id=other.id,
        tenant_id=tenant_id,
        created_at=_NOW,
    )
    monkeypatch.setattr(users_api.admin_audit_log_dao, "list_recent", AsyncMock(return_value=[rec]))
    logs = await users_api.list_org_admin_audit_logs(current_user=genesis)
    assert logs[0].action == "org_admin_create"


@pytest.mark.asyncio
async def test_create_org_admin_409_and_missing_tenant(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await users_api.create_org_admin(
            users_api.OrgAdminCreateRequest(admin_email="o@a.com", admin_password="secret1"),
            current_user=_user(role="org_admin", tenant_id=None),
        )
    assert exc.value.status_code == 403

    tenant_id = uuid.uuid4()
    monkeypatch.setattr(users_api, "is_genesis_org_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(
        users_api, "create_additional_org_admin", AsyncMock(side_effect=AdminEmailTakenError("o@a.com"))
    )
    with pytest.raises(HTTPException) as exc:
        await users_api.create_org_admin(
            users_api.OrgAdminCreateRequest(admin_email="o@a.com", admin_password="secret1"),
            current_user=_user(role="org_admin", tenant_id=tenant_id),
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_tenants_join_registration_and_me(monkeypatch):
    tenant = _tenant()
    user = _user(role="member", tenant_id=None)
    code = SimpleNamespace(tenant_id=tenant.id, used_count=0, max_uses=5, invitation_code="ABCD")
    monkeypatch.setattr("app.services.org_membership.require_active_invitation", AsyncMock(return_value=code))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr("app.services.org_membership.consume_invitation_code", AsyncMock())
    attached = _user(role="member", tenant_id=tenant.id)
    monkeypatch.setattr("app.services.org_membership.attach_user_to_org", AsyncMock(return_value=attached))
    result = await tenants_api.join_company(tenants_api.JoinRequest(invitation_code="ABCD"), user)
    assert result.role == "member"

    monkeypatch.setattr(tenants_api.system_setting_dao, "is_flag_enabled", AsyncMock(return_value=False))
    cfg = await tenants_api.get_registration_config()
    assert cfg["allow_self_create_company"] is False
    assert cfg["tenant_creation"] == "platform_admin_only"

    member = _user(role="member", tenant_id=tenant.id)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    mine = await tenants_api.get_my_tenant(member)
    assert mine.id == tenant.id

    monkeypatch.setattr(
        tenants_api.agent_dao,
        "token_usage_for_tenant",
        AsyncMock(
            return_value={
                "tokens_today": 10,
                "cache_today": 2,
                "cache_creation_today": 1,
                "tokens_month": 20,
                "cache_month": 4,
                "cache_creation_month": 2,
                "tokens_total": 30,
                "cache_total": 6,
                "cache_creation_total": 3,
            }
        ),
    )
    usage = await tenants_api.get_my_tenant_token_usage(member)
    assert usage["today"]["total_tokens"] == 10


@pytest.mark.asyncio
async def test_transfer_requires_correct_password_then_moves(monkeypatch):
    source = _tenant()
    dest = _tenant()
    member = _user(role="member", tenant_id=source.id)
    member.identity = SimpleNamespace(password_hash="hashed")
    code = SimpleNamespace(tenant_id=dest.id, used_count=0, max_uses=3)
    monkeypatch.setattr("app.services.org_membership.require_active_invitation", AsyncMock(return_value=code))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=dest))
    monkeypatch.setattr("app.services.org_membership.consume_invitation_code", AsyncMock())
    monkeypatch.setattr(
        "app.core.security.load_identity_for_password",
        AsyncMock(return_value=SimpleNamespace(password_hash="hashed")),
    )
    monkeypatch.setattr("app.core.security.verify_password_async", AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr("app.core.security.create_access_token", lambda *a, **k: "tok")
    moved = _user(role="member", tenant_id=dest.id)
    monkeypatch.setattr("app.services.org_membership.transfer_user_to_org", AsyncMock(return_value=moved))

    with pytest.raises(HTTPException) as denied:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="wrong", invitation_code="X"),
            member,
        )
    assert denied.value.status_code == 401

    out = await tenants_api.transfer_organization(
        tenants_api.TransferRequest(password="secret1", invitation_code="X"),
        member,
    )
    assert out.access_token == "tok"
    assert out.tenant.id == dest.id


@pytest.mark.asyncio
async def test_transfer_blocks_genesis(monkeypatch):
    genesis = _user(role="org_admin", tenant_id=uuid.uuid4(), is_genesis=True)
    genesis.identity = SimpleNamespace(password_hash="hashed")
    monkeypatch.setattr("app.core.security.verify_password_async", AsyncMock(return_value=True))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", invitation_code="X"),
            genesis,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_join_suggested_requires_verified_email_and_blocks_platform_admin(monkeypatch):
    tenant = _tenant()
    member = _user(role="member", tenant_id=None, email="ada@acme.com", email_verified=False)
    monkeypatch.setattr(
        "app.services.org_membership.lookup_tenant_for_verified_email",
        AsyncMock(return_value=None),
    )
    with pytest.raises(HTTPException) as unverified:
        await tenants_api.join_suggested_org(tenants_api.SuggestedJoinRequest(), member)
    assert unverified.value.status_code == 400

    pa = _user(role="platform_admin", tenant_id=None, email_verified=True)
    monkeypatch.setattr(
        "app.services.org_membership.lookup_tenant_for_verified_email",
        AsyncMock(return_value=tenant),
    )
    with pytest.raises(HTTPException) as blocked:
        await tenants_api.join_suggested_org(tenants_api.SuggestedJoinRequest(), pa)
    assert blocked.value.status_code == 403


@pytest.mark.asyncio
async def test_join_suggested_attaches_verified_member(monkeypatch):
    tenant = _tenant()
    member = _user(role="member", tenant_id=None, email="ada@acme.com")
    attached = _user(role="member", tenant_id=tenant.id)
    monkeypatch.setattr(
        "app.services.org_membership.lookup_tenant_for_verified_email",
        AsyncMock(return_value=tenant),
    )
    monkeypatch.setattr("app.services.org_membership.attach_user_to_org", AsyncMock(return_value=attached))
    monkeypatch.setattr("app.core.security.create_access_token", lambda *a, **k: "tok")
    out = await tenants_api.join_suggested_org(tenants_api.SuggestedJoinRequest(tenant_id=tenant.id), member)
    assert out.access_token == "tok"
    assert out.tenant.id == tenant.id


@pytest.mark.asyncio
async def test_delete_tenant_blocks_system_org(monkeypatch):
    tenant = _tenant(is_system=True)
    pa = _user(role="platform_admin")
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.delete_tenant(tenant.id, pa)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_update_me_clears_email_verified_and_checks_global_email(monkeypatch):
    from app.schemas.schemas import UserUpdate

    identity = SimpleNamespace(id=uuid.uuid4(), email="old@a.com", username="ada", phone=None, email_verified=True)
    user = _user(role="member", tenant_id=uuid.uuid4(), email="old@a.com")
    user.identity = identity
    captured: dict = {}

    async def _update_identity(*, db_obj, obj_in):
        captured.update(obj_in)
        return identity

    monkeypatch.setattr(auth_api.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(auth_api.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_api.user_dao, "get_by_email_and_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_api.identity_dao, "update", _update_identity)
    monkeypatch.setattr(auth_api.UserOut, "model_validate", staticmethod(lambda value: value))
    monkeypatch.setattr(
        "app.services.registration_service.registration_service.sync_org_member_contact_from_user",
        AsyncMock(),
    )

    await auth_api.update_me(UserUpdate(email="new@a.com"), user)
    assert captured["email"] == "new@a.com"
    assert captured["email_verified"] is False


@pytest.mark.asyncio
async def test_tenants_get_update_assign_delete(monkeypatch):
    tenant = _tenant()
    pa = _user(role="platform_admin")
    monkeypatch.setattr(tenants_api.tenant_dao, "list_ordered_by_created_at", AsyncMock(return_value=[tenant]))
    listed = await tenants_api.list_tenants(pa)
    assert listed[0].id == tenant.id

    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    got = await tenants_api.get_tenant(tenant.id, pa)
    assert got.name == "Acme"
    assert got.can_disable is True
    assert tenants_api.TenantOut.model_validate(_tenant(is_system=True)).can_disable is False
    assert tenants_api.TenantOut.model_validate(_tenant(is_default_end_user_org=True)).can_disable is False

    monkeypatch.setattr(tenants_api.tenant_dao, "update", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenants_api, "write_admin_audit", AsyncMock())
    updated = await tenants_api.update_tenant(tenant.id, tenants_api.TenantUpdate(name="Acme2"), pa)
    assert updated.id == tenant.id

    member = _user(role="member")
    monkeypatch.setattr(tenants_api.user_dao, "get_with_identity", AsyncMock(return_value=member))
    monkeypatch.setattr(tenants_api, "apply_user_assignment", AsyncMock(return_value=member))
    assigned = await tenants_api.assign_user_to_tenant(tenant.id, member.id, role="member", current_user=pa)
    assert assigned["role"] == "member"

    monkeypatch.setattr(tenants_api, "delete_tenant_and_release_identities", AsyncMock())
    monkeypatch.setattr(tenants_api.user_dao, "fallback_tenant_for_identity", AsyncMock(return_value=None))
    deleted = await tenants_api.delete_tenant(tenant.id, pa)
    assert deleted["status"] == "deleted"


@pytest.mark.asyncio
async def test_resolve_by_domain_404(monkeypatch):
    monkeypatch.setattr(tenants_api.system_setting_dao, "is_flag_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(tenants_api.tenant_dao, "get_by_slug", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.resolve_tenant_by_domain("unknown.example.com")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_provisioning_org_admin_and_activation_branches(monkeypatch):
    tenant = _tenant()
    identity = SimpleNamespace(id=uuid.uuid4())
    org = _user(role="org_admin", tenant_id=tenant.id)
    monkeypatch.setattr(provisioning.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(provisioning.identity_dao, "is_username_taken", AsyncMock(return_value=True))
    monkeypatch.setattr(provisioning, "hash_password_async", AsyncMock(return_value="h"))
    monkeypatch.setattr(provisioning.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(ValueError):
        await provisioning.create_additional_org_admin(
            tenant_id=tenant.id, admin_email="oa@acme.com", admin_password="secret1"
        )

    monkeypatch.setattr(provisioning.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(provisioning.identity_dao, "create_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(provisioning.user_dao, "create", AsyncMock(return_value=org))
    monkeypatch.setattr(provisioning.participant_dao, "create_for_user", AsyncMock())
    from unittest.mock import patch

    with (
        patch.object(provisioning, "connection_ctx") as ctx,
        patch("app.services.registration_service.registration_service.bind_org_member", AsyncMock()),
    ):
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await provisioning.create_additional_org_admin(
            tenant_id=tenant.id, admin_email="oa@acme.com", admin_password="secret1"
        )
    assert result.admin_email == "oa@acme.com"


@pytest.mark.asyncio
async def test_set_org_admin_active_happy(monkeypatch):
    tenant_id = uuid.uuid4()
    genesis = _user(role="org_admin", tenant_id=tenant_id)
    target = _user(role="org_admin", tenant_id=tenant_id, is_active=True)

    async def _is_genesis(user):
        return user.id == genesis.id

    monkeypatch.setattr(provisioning, "is_genesis_org_admin", _is_genesis)
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


@pytest.mark.asyncio
async def test_write_admin_audit_swallows_errors(monkeypatch):
    from app.services import admin_audit

    monkeypatch.setattr(admin_audit.admin_audit_log_dao, "create", AsyncMock(side_effect=RuntimeError("db")))
    await write_admin_audit(actor=_user(), action="x", target_type="user")


@pytest.mark.asyncio
async def test_admin_audit_dao_list_recent_sql(monkeypatch):
    from app.dao.admin_audit_dao import AdminAuditLogDAO

    dao = AdminAuditLogDAO()
    captured = {}

    class _Db:
        async def fetchall(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return []

    class _Ctx:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(dao, "session", lambda: _Ctx())
    await dao.list_recent(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), action="tenant_create", limit=5)
    assert "tenant_id" in captured["sql"]
    assert captured["params"]["limit"] == 5


@pytest.mark.asyncio
async def test_platform_metrics(monkeypatch):
    day = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 2, tzinfo=UTC)
    empty = {}
    monkeypatch.setattr(admin_api.tenant_dao, "counts_by_created_day", AsyncMock(return_value={day.date(): 1}))
    monkeypatch.setattr(admin_api.user_dao, "counts_by_created_day", AsyncMock(return_value=empty))
    monkeypatch.setattr(admin_api.agent_activity_log_dao, "tokens_by_day", AsyncMock(return_value={day.date(): 100}))
    monkeypatch.setattr(admin_api.agent_activity_log_dao, "cache_read_by_day", AsyncMock(return_value={day.date(): 10}))
    monkeypatch.setattr(admin_api.chat_session_dao, "counts_by_created_day", AsyncMock(return_value=empty))
    monkeypatch.setattr(admin_api.chat_session_dao, "dau_by_created_day", AsyncMock(return_value=empty))
    monkeypatch.setattr(admin_api.chat_session_dao, "wau_mau_by_day", AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(admin_api.tenant_dao, "count_created_before", AsyncMock(return_value=0))
    monkeypatch.setattr(admin_api.user_dao, "count_created_before", AsyncMock(return_value=0))
    monkeypatch.setattr(admin_api.agent_dao, "sum_tokens_created_before", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(admin_api.chat_session_dao, "count_created_before", AsyncMock(return_value=0))
    series = await admin_api.get_platform_timeseries(day, end, _user())
    assert series[0]["new_companies"] == 1
    assert series[0]["cache_hit_rate"] == 0.1

    monkeypatch.setattr(
        admin_api.agent_dao,
        "top_token_companies",
        AsyncMock(return_value=[{"name": "A", "total": 10, "cache_read": 2}]),
    )
    monkeypatch.setattr(
        admin_api.agent_dao,
        "top_token_agents",
        AsyncMock(return_value=[{"name": "ag", "company": "A", "tokens": 5, "cache_read_tokens": 1}]),
    )
    boards = await admin_api.get_platform_leaderboards(_user())
    assert boards["top_companies"][0]["cache_hit_rate"] == 0.2

    monkeypatch.setattr(admin_api.agent_activity_log_dao, "sum_tokens_since", AsyncMock(return_value=100))
    monkeypatch.setattr(admin_api.chat_session_dao, "count_created_since", AsyncMock(return_value=10))
    monkeypatch.setattr(admin_api.chat_session_dao, "retention_7d", AsyncMock(return_value=(10, 5)))
    monkeypatch.setattr(admin_api.chat_session_dao, "channel_distribution_since", AsyncMock(return_value={}))
    monkeypatch.setattr(admin_api.tool_dao, "top_enabled_categories", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin_api.chat_session_dao, "churn_warnings", AsyncMock(return_value=[]))
    enhanced = await admin_api.get_enhanced_metrics(_user())
    assert enhanced["retention_rate_7d"] == 50.0


def test_security_encrypt_roundtrip_and_empty():
    from app.core import security as sec

    assert sec.encrypt_data("", "k") == ""
    assert sec.decrypt_data("", "k") == ""
    token = sec.encrypt_data("hello", "secret-key")
    assert sec.decrypt_data(token, "secret-key") == "hello"
    with pytest.raises(ValueError):
        sec.decrypt_data("not-valid", "secret-key")
    jwt = sec.create_access_token(str(uuid.uuid4()), "member")
    assert sec.decode_access_token(jwt)["role"] == "member"
    with pytest.raises(HTTPException):
        sec.decode_access_token("bad.token.value")


@pytest.mark.asyncio
async def test_require_role_and_get_current_admin():
    from app.core import security as sec

    checker = sec.require_role("platform_admin")
    ok = await checker(_user(role="platform_admin"))
    assert ok.role == "platform_admin"
    with pytest.raises(HTTPException):
        await checker(_user(role="member"))
    with pytest.raises(HTTPException):
        await sec.get_current_admin(_user(role="member"))
    admin = await sec.get_current_admin(_user(role="org_admin"))
    assert admin.role == "org_admin"


@pytest.mark.asyncio
async def test_tenants_org_admin_scope_and_join_switch(monkeypatch):
    tenant = _tenant()
    org = _user(role="org_admin", tenant_id=tenant.id)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    got = await tenants_api.get_tenant(tenant.id, org)
    assert got.id == tenant.id
    with pytest.raises(HTTPException):
        await tenants_api.get_tenant(uuid.uuid4(), org)
    with pytest.raises(HTTPException):
        await tenants_api.get_tenant(tenant.id, _user(role="member", tenant_id=tenant.id))

    other = _user(role="member", tenant_id=uuid.uuid4())
    code = SimpleNamespace(tenant_id=tenant.id, used_count=0, max_uses=5)
    monkeypatch.setattr("app.services.org_membership.require_active_invitation", AsyncMock(return_value=code))
    monkeypatch.setattr(tenants_api.user_dao, "get_by_identity_and_tenant", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.join_company(tenants_api.JoinRequest(invitation_code="X"), other)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_seeder_unique_username_and_existing_platform_user(monkeypatch):
    from app.services import platform_admin_seeder as seeder

    monkeypatch.setattr(seeder.identity_dao, "is_username_taken", AsyncMock(side_effect=[True, False]))
    name = await seeder._unique_username("admin@example.com")
    assert name.startswith("admin_")

    identity = SimpleNamespace(id=uuid.uuid4(), username="admin")
    existing = _user(role="platform_admin", is_active=False, tenant_id=uuid.uuid4())
    existing.identity = None
    monkeypatch.setattr(
        seeder.tenant_dao,
        "get_by_slug",
        AsyncMock(return_value=_tenant(is_active=True, slug="maraclaw")),
    )
    monkeypatch.setattr(seeder, "_bind_directory", AsyncMock())
    monkeypatch.setattr(seeder.user_dao, "get_by_identity_id", AsyncMock(return_value=[existing]))
    monkeypatch.setattr(
        seeder.user_dao,
        "update",
        AsyncMock(side_effect=lambda db_obj, obj_in: SimpleNamespace(**{**db_obj.__dict__, **obj_in})),
    )
    got = await seeder._ensure_platform_user(identity)
    assert got.role == "platform_admin"


def test_tenant_helpers():
    tid = uuid.uuid4()
    assert tenants_api._tenant_logo_key(tid).endswith(".png")
    assert str(tid) in tenants_api._tenant_logo_url(tid)
    assert tenants_api._system_setting_enabled(None) is True
    assert tenants_api._system_setting_enabled({"enabled": False}) is False
    assert tenants_api._system_setting_enabled({"enabled": "x"}) is True


@pytest.mark.asyncio
async def test_get_updateable_tenant_and_join_errors(monkeypatch):
    tenant = _tenant()
    org = _user(role="org_admin", tenant_id=tenant.id)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    assert (await tenants_api._get_updateable_tenant(tenant.id, org)).id == tenant.id
    with pytest.raises(HTTPException):
        await tenants_api._get_updateable_tenant(uuid.uuid4(), org)
    with pytest.raises(HTTPException):
        await tenants_api._get_updateable_tenant(tenant.id, _user(role="member"))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await tenants_api._get_updateable_tenant(tenant.id, _user(role="platform_admin"))

    from app.services.org_membership import InvitationError

    monkeypatch.setattr(
        "app.services.org_membership.require_active_invitation",
        AsyncMock(side_effect=InvitationError("Invalid invitation code")),
    )
    with pytest.raises(HTTPException):
        await tenants_api.join_company(tenants_api.JoinRequest(invitation_code="NOPE"), _user(role="member"))

    monkeypatch.setattr(
        "app.services.org_membership.require_active_invitation",
        AsyncMock(side_effect=InvitationError("Invalid invitation code")),
    )
    with pytest.raises(HTTPException):
        await tenants_api.join_company(tenants_api.JoinRequest(invitation_code="X"), _user(role="member"))

    code = SimpleNamespace(tenant_id=tenant.id, used_count=0, max_uses=5)
    monkeypatch.setattr("app.services.org_membership.require_active_invitation", AsyncMock(return_value=code))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await tenants_api.join_company(tenants_api.JoinRequest(invitation_code="X"), _user(role="member"))

    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    with pytest.raises(HTTPException) as already:
        await tenants_api.join_company(
            tenants_api.JoinRequest(invitation_code="X"),
            _user(role="member", tenant_id=uuid.uuid4()),
        )
    assert already.value.status_code == 409


@pytest.mark.asyncio
async def test_resolve_domain_and_me_errors(monkeypatch):
    tenant = _tenant(is_active=True, sso_enabled=True, sso_domain="https://acme.maraclaw.ai")
    monkeypatch.setattr(tenants_api.system_setting_dao, "is_flag_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(tenants_api.tenant_dao, "get_by_sso_domain_exact", AsyncMock(return_value=tenant))
    resolved = await tenants_api.resolve_tenant_by_domain("acme.maraclaw.ai")
    assert resolved["slug"] == tenant.slug

    with pytest.raises(HTTPException):
        await tenants_api.get_my_tenant(_user(role="member", tenant_id=None))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await tenants_api.get_my_tenant(_user(role="member", tenant_id=uuid.uuid4()))
    with pytest.raises(HTTPException):
        await tenants_api.get_my_tenant_token_usage(_user(role="member", tenant_id=None))


@pytest.mark.asyncio
async def test_update_tenant_org_admin_and_assign_errors(monkeypatch):
    tenant = _tenant()
    org = _user(role="org_admin", tenant_id=tenant.id)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenants_api.tenant_dao, "update", AsyncMock(return_value=tenant))
    await tenants_api.update_tenant(tenant.id, tenants_api.TenantUpdate(name="N"), org)
    with pytest.raises(HTTPException):
        await tenants_api.update_tenant(uuid.uuid4(), tenants_api.TenantUpdate(name="N"), org)
    monkeypatch.setattr(tenants_api.user_dao, "get_with_identity", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await tenants_api.assign_user_to_tenant(tenant.id, uuid.uuid4(), role="member", current_user=_user())
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await tenants_api.assign_user_to_tenant(tenant.id, uuid.uuid4(), role="member", current_user=_user())


@pytest.mark.asyncio
async def test_user_role_remaining_branches(monkeypatch):
    tenant_id = uuid.uuid4()
    org = _user(role="org_admin", tenant_id=tenant_id)
    target = _user(role="member", tenant_id=tenant_id)
    with pytest.raises(HTTPException):
        await users_api.update_user_role(target.id, users_api.RoleUpdate(role="nope"), org)
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await users_api.update_user_role(target.id, users_api.RoleUpdate(role="member"), org)
    same = _user(role="member", tenant_id=tenant_id)
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=same))
    out = await users_api.update_user_role(same.id, users_api.RoleUpdate(role="member"), org)
    assert out["status"] == "ok"

    last = _user(role="org_admin", tenant_id=tenant_id)
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=last))
    monkeypatch.setattr(
        users_api,
        "apply_user_role_change",
        AsyncMock(side_effect=provisioning.AdminGuardError(400, "last admin")),
    )
    with pytest.raises(HTTPException) as exc:
        await users_api.update_user_role(last.id, users_api.RoleUpdate(role="member"), org)
    assert exc.value.status_code == 400

    pa = _user(role="platform_admin")
    last_pa = _user(role="platform_admin")
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=last_pa))
    monkeypatch.setattr(
        users_api,
        "apply_user_role_change",
        AsyncMock(side_effect=provisioning.AdminGuardError(400, "last pa")),
    )
    with pytest.raises(HTTPException) as exc:
        await users_api.update_user_role(last_pa.id, users_api.RoleUpdate(role="member"), pa)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_provisioning_last_active_and_non_admin(monkeypatch):
    genesis = _user(role="platform_admin")
    target = _user(role="platform_admin", is_active=True)

    async def _is_genesis(user):
        return user.id == genesis.id

    monkeypatch.setattr(provisioning, "is_genesis_platform_admin", _is_genesis)
    monkeypatch.setattr(provisioning.user_dao, "deactivate_unless_last_active", AsyncMock(return_value=None))
    with patch.object(provisioning, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        with pytest.raises(provisioning.AdminActivationError):
            await provisioning.set_peer_admin_active(actor=genesis, target=target, is_active=False)
    with pytest.raises(provisioning.AdminActivationError):
        await provisioning.set_peer_admin_active(actor=_user(role="member"), target=target, is_active=False)


@pytest.mark.asyncio
async def test_join_wrong_tenant_and_delete_logo(monkeypatch):
    tenant = _tenant()
    code = SimpleNamespace(tenant_id=tenant.id, used_count=0, max_uses=5)
    monkeypatch.setattr("app.services.org_membership.require_active_invitation", AsyncMock(return_value=code))
    with pytest.raises(HTTPException):
        await tenants_api.join_company(
            tenants_api.JoinRequest(invitation_code="X", target_tenant_id=uuid.uuid4()),
            _user(role="member"),
        )

    org = _user(role="org_admin", tenant_id=tenant.id)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    storage = SimpleNamespace(exists=AsyncMock(return_value=False), delete=AsyncMock())
    monkeypatch.setattr(tenants_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(tenants_api.tenant_dao, "update", AsyncMock(return_value=tenant))
    out = await tenants_api.delete_tenant_logo(tenant.id, org)
    assert out.id == tenant.id

    org_no_tenant = _user(role="org_admin", tenant_id=None)
    with pytest.raises(HTTPException):
        await tenants_api.get_tenant(tenant.id, org_no_tenant)
    with pytest.raises(HTTPException):
        await tenants_api.update_tenant(tenant.id, tenants_api.TenantUpdate(name="Z"), org_no_tenant)


@pytest.mark.asyncio
async def test_provisioning_noop_active(monkeypatch):
    genesis = _user(role="platform_admin")
    target = _user(role="platform_admin", is_active=False)

    async def _is_genesis(user):
        return user.id == genesis.id

    monkeypatch.setattr(provisioning, "is_genesis_platform_admin", _is_genesis)
    same = await provisioning.set_peer_admin_active(actor=genesis, target=target, is_active=False)
    assert same.is_active is False

    identity = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    active_target = _user(role="platform_admin", is_active=True)
    active_target.identity = identity
    monkeypatch.setattr(
        provisioning.user_dao,
        "deactivate_unless_last_active",
        AsyncMock(return_value=SimpleNamespace(**{**active_target.__dict__, "is_active": False})),
    )
    monkeypatch.setattr(provisioning.identity_dao, "update", AsyncMock())
    monkeypatch.setattr(provisioning, "write_admin_audit", AsyncMock())
    with patch.object(provisioning, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        flipped = await provisioning.set_peer_admin_active(actor=genesis, target=active_target, is_active=False)
    assert flipped.is_active is False


@pytest.mark.asyncio
async def test_get_tenant_logo_missing(monkeypatch):
    storage = SimpleNamespace(exists=AsyncMock(return_value=False))
    monkeypatch.setattr(tenants_api, "get_storage_backend", lambda: storage)
    with pytest.raises(HTTPException):
        await tenants_api.get_tenant_logo(uuid.uuid4())


@pytest.mark.asyncio
async def test_load_user_from_access_token_branches(monkeypatch):
    from app.core import security as sec

    token = sec.create_access_token(str(uuid.uuid4()), "member")
    monkeypatch.setattr(sec.user_dao, "get_with_identity", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await sec.load_user_from_access_token(token)
    inactive = _user(role="member", is_active=False)
    monkeypatch.setattr(sec.user_dao, "get_with_identity", AsyncMock(return_value=inactive))
    with pytest.raises(HTTPException):
        await sec.load_user_from_access_token(token, require_active=True)
    with pytest.raises(HTTPException):
        await sec.load_user_from_access_token("not-a-jwt")


@pytest.mark.asyncio
async def test_join_refuses_genesis_platform_admin_rewrite(monkeypatch):
    tenant = _tenant()
    genesis = _user(role="platform_admin", tenant_id=None, is_genesis=True)
    code = SimpleNamespace(tenant_id=tenant.id, used_count=0, max_uses=5)
    monkeypatch.setattr("app.services.org_membership.require_active_invitation", AsyncMock(return_value=code))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenants_api.user_dao, "get_by_identity_and_tenant", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.join_company(tenants_api.JoinRequest(invitation_code="ABCD"), genesis)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_repair_genesis_org_admin(monkeypatch):
    tenant = _tenant()
    pa = _user(role="platform_admin", is_genesis=True)
    oa = _user(role="org_admin", tenant_id=tenant.id, is_genesis=True)
    monkeypatch.setattr(tenants_api, "is_genesis_platform_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        tenants_api,
        "attach_genesis_org_admin",
        AsyncMock(return_value=SimpleNamespace(tenant=tenant, org_admin=oa, admin_email="oa@acme.com")),
    )
    monkeypatch.setattr(tenants_api, "write_admin_audit", AsyncMock())
    result = await tenants_api.repair_genesis_org_admin(
        tenant.id,
        tenants_api.GenesisOrgAdminCreate(admin_email="oa@acme.com", admin_password="secret1"),
        pa,
    )
    assert result.org_admin_email == "oa@acme.com"
    assert result.must_change_password is True

    monkeypatch.setattr(tenants_api, "is_genesis_platform_admin", AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.repair_genesis_org_admin(
            tenant.id,
            tenants_api.GenesisOrgAdminCreate(admin_email="oa@acme.com", admin_password="secret1"),
            _user(role="platform_admin"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_repair_genesis_org_admin_conflict(monkeypatch):
    from app.services.tenant_provisioning import GenesisOrgAdminExistsError

    tenant = _tenant()
    monkeypatch.setattr(tenants_api, "is_genesis_platform_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenants_api, "attach_genesis_org_admin", AsyncMock(side_effect=GenesisOrgAdminExistsError()))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.repair_genesis_org_admin(
            tenant.id,
            tenants_api.GenesisOrgAdminCreate(admin_email="oa@acme.com", admin_password="secret1"),
            _user(role="platform_admin", is_genesis=True),
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_upload_tenant_logo_happy_path(monkeypatch):
    from io import BytesIO

    from PIL import Image

    tenant = _tenant(im_config={})
    org = _user(role="org_admin", tenant_id=tenant.id)
    image = Image.new("RGB", (32, 32), color="red")
    buf = BytesIO()
    image.save(buf, format="PNG")
    payload = buf.getvalue()
    upload = SimpleNamespace(content_type="image/png", read=AsyncMock(return_value=payload))
    storage = SimpleNamespace(write_bytes=AsyncMock())
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenants_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(tenants_api.tenant_dao, "update", AsyncMock(return_value=tenant))
    out = await tenants_api.upload_tenant_logo(tenant.id, upload, org)
    assert out.id == tenant.id
    storage.write_bytes.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_skips_inactive_memberships(monkeypatch):
    from app.api import auth as auth_api

    identity = SimpleNamespace(
        id=uuid.uuid4(),
        email="oa@acme.com",
        password_hash="hashed",
        is_active=True,
        email_verified=True,
        must_change_password=False,
        username="oa",
        phone=None,
        is_platform_admin=False,
        created_at=_NOW,
        updated_at=_NOW,
    )
    inactive = _user(role="org_admin", is_active=False, tenant_id=uuid.uuid4())
    active = _user(role="member", is_active=True, tenant_id=uuid.uuid4())
    monkeypatch.setattr(auth_api.identity_dao, "get_by_login_identifier", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth_api, "verify_password_async", AsyncMock(return_value=True))
    monkeypatch.setattr(auth_api.user_dao, "get_by_identity_id", AsyncMock(return_value=[inactive]))
    with pytest.raises(HTTPException) as exc:
        await auth_api.login(
            auth_api.UserLogin(login_identifier="oa@acme.com", password="secret1"),
            background_tasks=SimpleNamespace(),
            request=Request(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/",
                    "raw_path": b"/",
                    "query_string": b"",
                    "headers": [],
                    "client": ("127.0.0.1", 1),
                    "server": ("127.0.0.1", 8000),
                }
            ),
        )
    assert exc.value.status_code == 403

    monkeypatch.setattr(auth_api.user_dao, "get_by_identity_id", AsyncMock(return_value=[inactive, active]))
    monkeypatch.setattr(auth_api.tenant_dao, "get", AsyncMock(return_value=_tenant(id=active.tenant_id)))
    result = await auth_api.login(
        auth_api.UserLogin(login_identifier="oa@acme.com", password="secret1"),
        background_tasks=SimpleNamespace(),
        request=Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 8000),
            }
        ),
    )
    assert result.user.id == active.id


@pytest.mark.asyncio
async def test_logo_and_repair_error_branches(monkeypatch):
    tenant = _tenant()
    org = _user(role="org_admin", tenant_id=tenant.id)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))

    bad_type = SimpleNamespace(content_type="text/plain", read=AsyncMock(return_value=b"x"))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.upload_tenant_logo(tenant.id, bad_type, org)
    assert exc.value.status_code == 400

    huge = SimpleNamespace(content_type="image/png", read=AsyncMock(return_value=b"x" * (1024 * 1024 + 1)))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.upload_tenant_logo(tenant.id, huge, org)
    assert exc.value.status_code == 400

    invalid = SimpleNamespace(content_type="image/png", read=AsyncMock(return_value=b"not-an-image"))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.upload_tenant_logo(tenant.id, invalid, org)
    assert exc.value.status_code == 400

    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (32, 16), color="red")
    buf = BytesIO()
    image.save(buf, format="PNG")
    rect = SimpleNamespace(content_type="image/png", read=AsyncMock(return_value=buf.getvalue()))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.upload_tenant_logo(tenant.id, rect, org)
    assert exc.value.status_code == 400

    storage = SimpleNamespace(exists=AsyncMock(return_value=True))
    monkeypatch.setattr(tenants_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(tenants_api, "ensure_local_path", AsyncMock(return_value="/tmp/logo.png"))
    logo = await tenants_api.get_tenant_logo(tenant.id)
    assert logo.path == "/tmp/logo.png"

    monkeypatch.setattr(tenants_api.user_dao, "get_with_identity", AsyncMock(return_value=_user(role="member")))
    monkeypatch.setattr(
        tenants_api,
        "apply_user_assignment",
        AsyncMock(side_effect=provisioning.AdminGuardError(403, "genesis")),
    )
    with pytest.raises(HTTPException) as exc:
        await tenants_api.assign_user_to_tenant(tenant.id, uuid.uuid4(), role="member", current_user=_user())
    assert exc.value.status_code == 403

    monkeypatch.setattr(tenants_api, "is_genesis_platform_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.repair_genesis_org_admin(
            tenant.id,
            tenants_api.GenesisOrgAdminCreate(admin_email="oa@acme.com", admin_password="secret1"),
            _user(role="platform_admin", is_genesis=True),
        )
    assert exc.value.status_code == 404

    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenants_api, "attach_genesis_org_admin", AsyncMock(side_effect=AdminEmailTakenError("x")))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.repair_genesis_org_admin(
            tenant.id,
            tenants_api.GenesisOrgAdminCreate(admin_email="oa@acme.com", admin_password="secret1"),
            _user(role="platform_admin", is_genesis=True),
        )
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        await tenants_api.delete_tenant(tenant.id, _user(role="member"))
    assert exc.value.status_code == 403
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.delete_tenant(tenant.id, _user(role="platform_admin"))
    assert exc.value.status_code == 404

    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await tenants_api.update_tenant(tenant.id, tenants_api.TenantUpdate(name="N"), _user())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_tenants_client_ip_and_lookup_by_email(monkeypatch):
    req = MagicMock(spec=Request)
    req.client = None
    assert await tenants_api.get_client_ip(req) is None
    req.client = SimpleNamespace(host="203.0.113.9")
    assert await tenants_api.get_client_ip(req) == "203.0.113.9"

    match = _tenant(name="Acme", slug="acme")
    fallback = _tenant(name="OpenClaw", slug="openclaw")
    monkeypatch.setattr("app.services.org_membership.lookup_tenant_by_email_domain", AsyncMock(return_value=match))
    monkeypatch.setattr("app.services.org_membership.get_fallback_org", AsyncMock(return_value=fallback))
    out = await tenants_api.lookup_org_by_email("ada@acme.com")
    assert out.match is not None
    assert out.match.slug == "acme"
    assert out.fallback is not None
    assert out.fallback.slug == "openclaw"

    from app.services.org_membership import DefaultOrgUnavailableError

    monkeypatch.setattr("app.services.org_membership.lookup_tenant_by_email_domain", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.org_membership.get_fallback_org",
        AsyncMock(side_effect=DefaultOrgUnavailableError("missing")),
    )
    empty = await tenants_api.lookup_org_by_email("nobody@example.com")
    assert empty.match is None
    assert empty.fallback is None


@pytest.mark.asyncio
async def test_join_default_and_suggested_remaining_branches(monkeypatch):
    from app.services.org_membership import AlreadyInOrgError, DefaultOrgUnavailableError

    fallback = _tenant(name="OpenClaw", slug="openclaw")
    member = _user(role="member", tenant_id=None)
    attached = _user(role="member", tenant_id=fallback.id)
    monkeypatch.setattr("app.services.org_membership.get_fallback_org", AsyncMock(return_value=fallback))
    monkeypatch.setattr("app.services.org_membership.attach_user_to_org", AsyncMock(return_value=attached))
    monkeypatch.setattr("app.core.security.create_access_token", lambda *a, **k: "tok")
    joined = await tenants_api.join_default_org(member)
    assert joined.access_token == "tok"
    assert joined.tenant.id == fallback.id

    with pytest.raises(HTTPException) as already:
        await tenants_api.join_default_org(_user(role="member", tenant_id=uuid.uuid4()))
    assert already.value.status_code == 409

    monkeypatch.setattr(
        "app.services.org_membership.get_fallback_org",
        AsyncMock(side_effect=DefaultOrgUnavailableError("no default")),
    )
    with pytest.raises(HTTPException) as missing:
        await tenants_api.join_default_org(_user(role="member", tenant_id=None))
    assert missing.value.status_code == 503

    monkeypatch.setattr("app.services.org_membership.get_fallback_org", AsyncMock(return_value=fallback))
    monkeypatch.setattr(
        "app.services.org_membership.attach_user_to_org",
        AsyncMock(side_effect=AlreadyInOrgError("already")),
    )
    with pytest.raises(HTTPException) as conflict:
        await tenants_api.join_default_org(_user(role="member", tenant_id=None))
    assert conflict.value.status_code == 409

    suggested = _tenant()
    with pytest.raises(HTTPException) as in_org:
        await tenants_api.join_suggested_org(
            tenants_api.SuggestedJoinRequest(),
            _user(role="member", tenant_id=uuid.uuid4()),
        )
    assert in_org.value.status_code == 409

    monkeypatch.setattr("app.services.org_membership.lookup_tenant_for_verified_email", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as no_match:
        await tenants_api.join_suggested_org(tenants_api.SuggestedJoinRequest(), _user(role="member", tenant_id=None))
    assert no_match.value.status_code == 400

    monkeypatch.setattr(
        "app.services.org_membership.lookup_tenant_for_verified_email",
        AsyncMock(return_value=suggested),
    )
    with pytest.raises(HTTPException) as mismatch:
        await tenants_api.join_suggested_org(
            tenants_api.SuggestedJoinRequest(tenant_id=uuid.uuid4()),
            _user(role="member", tenant_id=None),
        )
    assert mismatch.value.status_code == 400

    monkeypatch.setattr(
        "app.services.org_membership.attach_user_to_org",
        AsyncMock(side_effect=AlreadyInOrgError("already")),
    )
    with pytest.raises(HTTPException) as suggested_conflict:
        await tenants_api.join_suggested_org(
            tenants_api.SuggestedJoinRequest(tenant_id=suggested.id),
            _user(role="member", tenant_id=None),
        )
    assert suggested_conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_join_company_invitation_without_org_and_attach_conflict(monkeypatch):
    from app.services.org_membership import AlreadyInOrgError

    tenant = _tenant()
    monkeypatch.setattr(
        "app.services.org_membership.require_active_invitation",
        AsyncMock(return_value=SimpleNamespace(tenant_id=None)),
    )
    with pytest.raises(HTTPException) as no_org:
        await tenants_api.join_company(tenants_api.JoinRequest(invitation_code="X"), _user(role="member"))
    assert no_org.value.status_code == 400

    monkeypatch.setattr(
        "app.services.org_membership.require_active_invitation",
        AsyncMock(return_value=SimpleNamespace(tenant_id=tenant.id)),
    )
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        "app.services.org_membership.attach_user_to_org",
        AsyncMock(side_effect=AlreadyInOrgError("already")),
    )
    with pytest.raises(HTTPException) as conflict:
        await tenants_api.join_company(tenants_api.JoinRequest(invitation_code="X"), _user(role="member"))
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_transfer_remaining_error_and_tenant_id_paths(monkeypatch):
    from app.services.org_membership import AlreadyInOrgError, DefaultOrgUnavailableError, InvitationError

    dest = _tenant()
    source = _tenant()
    member = _user(role="member", tenant_id=source.id)
    member.identity = SimpleNamespace(id=member.identity_id, password_hash="hashed")
    monkeypatch.setattr(
        "app.core.security.load_identity_for_password",
        AsyncMock(return_value=SimpleNamespace(password_hash="hashed")),
    )
    monkeypatch.setattr("app.core.security.verify_password_async", AsyncMock(return_value=True))
    monkeypatch.setattr("app.core.security.create_access_token", lambda *a, **k: "tok")

    with pytest.raises(HTTPException) as no_tenant:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", invitation_code="X"),
            _user(role="member", tenant_id=None),
        )
    assert no_tenant.value.status_code == 400

    monkeypatch.setattr("app.core.security.load_identity_for_password", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as no_identity:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", invitation_code="X"),
            member,
        )
    assert no_identity.value.status_code == 400

    monkeypatch.setattr(
        "app.core.security.load_identity_for_password",
        AsyncMock(return_value=SimpleNamespace(password_hash="hashed")),
    )
    monkeypatch.setattr(
        "app.services.org_membership.require_active_invitation",
        AsyncMock(side_effect=InvitationError("bad code")),
    )
    with pytest.raises(HTTPException) as bad_invite:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", invitation_code="X"),
            member,
        )
    assert bad_invite.value.status_code == 400

    monkeypatch.setattr(
        "app.services.org_membership.require_active_invitation",
        AsyncMock(return_value=SimpleNamespace(tenant_id=dest.id)),
    )
    with pytest.raises(HTTPException) as mismatch:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", invitation_code="X", tenant_id=uuid.uuid4()),
            member,
        )
    assert mismatch.value.status_code == 403

    monkeypatch.setattr(
        "app.services.org_membership.require_active_invitation",
        AsyncMock(return_value=SimpleNamespace(tenant_id=None)),
    )
    with pytest.raises(HTTPException) as invite_no_org:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", invitation_code="X"),
            member,
        )
    assert invite_no_org.value.status_code == 400

    monkeypatch.setattr(
        "app.services.org_membership.require_active_invitation",
        AsyncMock(return_value=SimpleNamespace(tenant_id=dest.id)),
    )
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as missing_dest:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", invitation_code="X"),
            member,
        )
    assert missing_dest.value.status_code == 400

    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=source))
    with pytest.raises(HTTPException) as same_org:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", invitation_code="X"),
            member,
        )
    assert same_org.value.status_code == 400

    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=dest))
    monkeypatch.setattr(
        "app.services.org_membership.lookup_tenant_for_verified_email",
        AsyncMock(return_value=dest),
    )
    monkeypatch.setattr("app.services.org_membership.get_fallback_org", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.org_membership.transfer_user_to_org",
        AsyncMock(return_value=_user(role="member", tenant_id=dest.id)),
    )
    moved = await tenants_api.transfer_organization(
        tenants_api.TransferRequest(password="secret1", tenant_id=dest.id),
        member,
    )
    assert moved.access_token == "tok"

    monkeypatch.setattr(
        "app.services.org_membership.lookup_tenant_for_verified_email",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.org_membership.get_fallback_org",
        AsyncMock(side_effect=DefaultOrgUnavailableError("none")),
    )
    with pytest.raises(HTTPException) as not_allowed:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", tenant_id=dest.id),
            member,
        )
    assert not_allowed.value.status_code == 403

    monkeypatch.setattr(
        "app.services.org_membership.lookup_tenant_for_verified_email",
        AsyncMock(return_value=dest),
    )
    monkeypatch.setattr("app.services.org_membership.get_fallback_org", AsyncMock(return_value=dest))
    monkeypatch.setattr(
        "app.services.org_membership.transfer_user_to_org",
        AsyncMock(side_effect=AlreadyInOrgError("already")),
    )
    with pytest.raises(HTTPException) as transfer_conflict:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", tenant_id=dest.id),
            member,
        )
    assert transfer_conflict.value.status_code == 403

    monkeypatch.setattr(
        "app.services.org_membership.transfer_user_to_org",
        AsyncMock(side_effect=DefaultOrgUnavailableError("gone")),
    )
    with pytest.raises(HTTPException) as unavailable:
        await tenants_api.transfer_organization(
            tenants_api.TransferRequest(password="secret1", tenant_id=dest.id),
            member,
        )
    assert unavailable.value.status_code == 400

    with pytest.raises(HTTPException) as no_target:
        await tenants_api.transfer_organization(tenants_api.TransferRequest(password="secret1"), member)
    assert no_target.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_domain_port_and_slug_fallbacks(monkeypatch):
    tenant = _tenant(is_active=True, sso_enabled=True, sso_domain="http://1.2.3.4")
    monkeypatch.setattr(tenants_api.system_setting_dao, "is_flag_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(tenants_api.tenant_dao, "get_by_sso_domain_exact", AsyncMock(return_value=None))
    monkeypatch.setattr(tenants_api.tenant_dao, "get_by_sso_domain_like", AsyncMock(return_value=tenant))
    resolved = await tenants_api.resolve_tenant_by_domain("1.2.3.4:3009")
    assert resolved["id"] == tenant.id

    slug_tenant = _tenant(is_active=True, sso_enabled=True, slug="acme")
    monkeypatch.setattr(tenants_api.tenant_dao, "get_by_sso_domain_like", AsyncMock(return_value=None))
    monkeypatch.setattr(tenants_api.tenant_dao, "get_by_slug", AsyncMock(return_value=slug_tenant))
    by_slug = await tenants_api.resolve_tenant_by_domain("acme.maraclaw.ai")
    assert by_slug["slug"] == "acme"


@pytest.mark.asyncio
async def test_email_domain_crud_and_get_tenant_missing(monkeypatch):
    from app.dao.tenant_email_domain_dao import tenant_email_domain_dao
    from app.services.org_membership import DomainClaimedError, InvalidEmailDomainError

    tenant = _tenant()
    pa = _user(role="platform_admin")
    domain = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant.id, domain="acme.com", is_default=True, created_at=_NOW)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenant_email_domain_dao, "list_for_tenant", AsyncMock(return_value=[domain]))
    listed = await tenants_api.list_email_domains(tenant.id, pa)
    assert listed[0].domain == "acme.com"

    monkeypatch.setattr("app.services.org_membership.add_email_domain", AsyncMock(return_value=domain))
    monkeypatch.setattr(tenants_api, "write_admin_audit", AsyncMock())
    created = await tenants_api.create_email_domain(
        tenant.id, tenants_api.EmailDomainCreate(domain="acme.com", is_default=True), pa, "10.0.0.1"
    )
    assert created.domain == "acme.com"

    monkeypatch.setattr(
        "app.services.org_membership.add_email_domain",
        AsyncMock(side_effect=InvalidEmailDomainError("bad")),
    )
    with pytest.raises(HTTPException) as invalid:
        await tenants_api.create_email_domain(tenant.id, tenants_api.EmailDomainCreate(domain="nope"), pa, None)
    assert invalid.value.status_code == 400

    monkeypatch.setattr(
        "app.services.org_membership.add_email_domain",
        AsyncMock(side_effect=DomainClaimedError("taken")),
    )
    with pytest.raises(HTTPException) as claimed:
        await tenants_api.create_email_domain(tenant.id, tenants_api.EmailDomainCreate(domain="acme.com"), pa, None)
    assert claimed.value.status_code == 409

    monkeypatch.setattr("app.services.org_membership.set_default_email_domain", AsyncMock(return_value=domain))
    patched = await tenants_api.patch_email_domain(
        tenant.id, domain.id, tenants_api.EmailDomainPatch(is_default=True), pa, None
    )
    assert patched.is_default is True
    with pytest.raises(HTTPException) as clear:
        await tenants_api.patch_email_domain(
            tenant.id, domain.id, tenants_api.EmailDomainPatch(is_default=False), pa, None
        )
    assert clear.value.status_code == 400
    monkeypatch.setattr("app.services.org_membership.set_default_email_domain", AsyncMock(side_effect=KeyError("x")))
    with pytest.raises(HTTPException) as missing_domain:
        await tenants_api.patch_email_domain(
            tenant.id, domain.id, tenants_api.EmailDomainPatch(is_default=True), pa, None
        )
    assert missing_domain.value.status_code == 404

    monkeypatch.setattr("app.services.org_membership.delete_email_domain", AsyncMock())
    await tenants_api.remove_email_domain(tenant.id, domain.id, pa, None)
    monkeypatch.setattr("app.services.org_membership.delete_email_domain", AsyncMock(side_effect=KeyError("x")))
    with pytest.raises(HTTPException) as missing_delete:
        await tenants_api.remove_email_domain(tenant.id, domain.id, pa, None)
    assert missing_delete.value.status_code == 404

    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as missing_tenant:
        await tenants_api.get_tenant(tenant.id, pa)
    assert missing_tenant.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_provisioning_taken_and_assignment_guards(monkeypatch):
    from app.db.errors import UniqueViolationError

    monkeypatch.setattr(
        provisioning.identity_dao, "get_by_email", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    with pytest.raises(AdminEmailTakenError):
        await provisioning.create_additional_platform_admin(admin_email="pa@acme.com", admin_password="secret1")

    monkeypatch.setattr(provisioning.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(provisioning.identity_dao, "is_username_taken", AsyncMock(return_value=False))
    monkeypatch.setattr(provisioning, "hash_password_async", AsyncMock(return_value="h"))
    monkeypatch.setattr(
        provisioning.identity_dao, "create_identity", AsyncMock(side_effect=UniqueViolationError("dup"))
    )
    with patch.object(provisioning, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        with pytest.raises(AdminEmailTakenError):
            await provisioning.create_additional_platform_admin(admin_email="pa@acme.com", admin_password="secret1")

    actor = _user(role="member")
    target = _user(role="member")
    with pytest.raises(provisioning.AdminGuardError):
        await provisioning.apply_user_role_change(actor=actor, target=target, new_role="member")
    with pytest.raises(provisioning.AdminGuardError):
        await provisioning.apply_user_role_change(actor=_user(role="org_admin"), target=target, new_role="nope")
    with pytest.raises(provisioning.AdminGuardError):
        await provisioning.apply_user_assignment(
            actor=_user(), target=_user(is_genesis=True), tenant_id=uuid.uuid4(), role="member"
        )
    with pytest.raises(provisioning.AdminGuardError):
        await provisioning.apply_user_assignment(actor=_user(), target=target, tenant_id=uuid.uuid4(), role="org_admin")

    pa = _user(role="platform_admin")
    same = _user(role="member", tenant_id=uuid.uuid4())
    unchanged = await provisioning.apply_user_role_change(actor=pa, target=same, new_role="member")
    assert unchanged.id == same.id

    genesis_target = _user(role="member", is_genesis=True)
    with pytest.raises(provisioning.AdminGuardError):
        await provisioning.apply_user_role_change(actor=pa, target=genesis_target, new_role="org_admin")

    monkeypatch.setattr(provisioning, "is_genesis_platform_admin", AsyncMock(return_value=False))
    with pytest.raises(provisioning.AdminGuardError):
        await provisioning.apply_user_role_change(actor=pa, target=target, new_role="platform_admin")

    org = _user(role="org_admin", tenant_id=uuid.uuid4())
    monkeypatch.setattr(provisioning, "is_genesis_org_admin", AsyncMock(return_value=False))
    with pytest.raises(provisioning.AdminGuardError):
        await provisioning.apply_user_role_change(actor=org, target=target, new_role="org_admin")

    last_pa = _user(role="platform_admin", is_active=True)
    monkeypatch.setattr(provisioning.user_dao, "count_active_by_role", AsyncMock(return_value=1))
    with pytest.raises(provisioning.AdminGuardError):
        await provisioning._assert_not_last_active_admin(last_pa, leaving_role="platform_admin")
    last_oa = _user(role="org_admin", tenant_id=uuid.uuid4(), is_active=True)
    with pytest.raises(provisioning.AdminGuardError):
        await provisioning._assert_not_last_active_admin(last_oa, leaving_role="org_admin")
    inactive = _user(role="platform_admin", is_active=False)
    await provisioning._assert_not_last_active_admin(inactive, leaving_role="platform_admin")


@pytest.mark.asyncio
async def test_seeder_and_tenant_provisioning_remaining(monkeypatch):
    from app.services import platform_admin_seeder as seeder, tenant_provisioning as tp

    monkeypatch.setattr(seeder.tenant_dao, "get_by_slug", AsyncMock(return_value=None))
    with pytest.raises(seeder.PlatformAdminSeedError):
        await seeder._require_maraclaw()

    monkeypatch.setattr(seeder.identity_dao, "is_username_taken", AsyncMock(return_value=True))
    generated = await seeder._unique_username("admin@example.com")
    assert generated.startswith("admin_")

    identity = SimpleNamespace(id=uuid.uuid4(), email="gpa@x.com", password_hash="h")
    user = _user(role="platform_admin")
    user.identity = SimpleNamespace(id=identity.id, email="gpa@x.com", password_hash=None)
    user.identity_id = identity.id
    monkeypatch.setattr(seeder.identity_dao, "get", AsyncMock(return_value=identity))
    rehydrated = await seeder._rehydrate_identity_hash(user)
    assert rehydrated.identity.password_hash == "h"
    assert seeder._has_login_credentials(rehydrated) is True
    assert seeder._has_login_credentials(_user()) is False

    monkeypatch.setattr(seeder.user_dao, "genesis_platform_admin", AsyncMock(return_value=None))
    earliest = _user(role="platform_admin", is_genesis=False)
    monkeypatch.setattr(seeder.user_dao, "first_by_role", AsyncMock(return_value=earliest))
    monkeypatch.setattr(
        seeder.user_dao,
        "update",
        AsyncMock(side_effect=lambda db_obj, obj_in: SimpleNamespace(**{**db_obj.__dict__, **obj_in})),
    )
    found = await seeder._find_genesis_membership()
    assert found is not None
    assert found.is_genesis is True

    empty_slug = tp.slugify_tenant_name("   ")
    assert "-" in empty_slug

    monkeypatch.setattr(tp.identity_dao, "get_by_email", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())))
    with pytest.raises(AdminEmailTakenError):
        await tp.create_tenant_with_org_admin(name="Acme", admin_email="oa@acme.com", admin_password="secret1")

    monkeypatch.setattr(tp.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(tp.identity_dao, "is_username_taken", AsyncMock(return_value=True))
    monkeypatch.setattr(tp, "hash_password_async", AsyncMock(return_value="h"))
    monkeypatch.setattr(tp.tenant_dao, "create", AsyncMock(side_effect=RuntimeError("stop-after-username")))
    with patch.object(tp, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        with pytest.raises(RuntimeError):
            await tp.create_tenant_with_org_admin(name="Acme", admin_email="oa@acme.com", admin_password="secret1")


@pytest.mark.asyncio
async def test_enterprise_stats_org_admin_cannot_read_other_tenant(monkeypatch):
    org = _user(role="org_admin", tenant_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await enterprise_api.get_enterprise_stats(tenant_id=str(uuid.uuid4()), current_user=org)
    assert exc.value.status_code == 403

    orphan = _user(role="org_admin", tenant_id=None)
    with pytest.raises(HTTPException) as missing:
        await enterprise_api.get_enterprise_stats(current_user=orphan)
    assert missing.value.status_code == 403

    own = uuid.uuid4()
    scoped = _user(role="org_admin", tenant_id=own)
    monkeypatch.setattr(enterprise_api.agent_dao, "count_for_tenant", AsyncMock(return_value=3))
    monkeypatch.setattr(enterprise_api.user_dao, "count_active", AsyncMock(return_value=2))
    monkeypatch.setattr(enterprise_api.approval_request_dao, "count_pending", AsyncMock(return_value=1))
    stats = await enterprise_api.get_enterprise_stats(tenant_id=str(own), current_user=scoped)
    assert stats["total_agents"] == 3
    assert stats["total_users"] == 2
    assert stats["pending_approvals"] == 1
