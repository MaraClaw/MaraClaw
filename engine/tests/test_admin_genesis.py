"""Tests for genesis platform admin + org admin administration rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import admin as admin_api, auth as auth_api
from app.core import security as security_mod
from app.core.security import hash_password
from app.records.identity import IdentityRecord
from app.records.user import UserRecord

_NOW = datetime.now(UTC)
_DEFAULT_TEST_PASSWORD = "initial-password"
_MARACLAW_ID = uuid.uuid4()


def _patch_maraclaw(monkeypatch, seeder, *, tenant_id=None):
    tid = tenant_id or _MARACLAW_ID
    tenant = SimpleNamespace(id=tid, slug="maraclaw", name="MaraClaw", is_active=True)
    monkeypatch.setattr(seeder.tenant_dao, "get_by_slug", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        "app.services.registration_service.registration_service.bind_org_member",
        AsyncMock(),
    )
    return tenant


def _identity_record(
    *,
    email="admin@example.com",
    password=_DEFAULT_TEST_PASSWORD,
    must_change_password=True,
    is_platform_admin=True,
    is_active=True,
):
    return IdentityRecord(
        id=uuid.uuid4(),
        email=email,
        username="admin",
        phone=None,
        password_hash=hash_password(password),
        is_active=is_active,
        email_verified=True,
        is_platform_admin=is_platform_admin,
        must_change_password=must_change_password,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _user_record(identity: IdentityRecord, *, role="platform_admin", tenant_id=None, is_genesis=None):
    return UserRecord(
        id=uuid.uuid4(),
        identity_id=identity.id,
        tenant_id=tenant_id,
        display_name="Admin",
        role=role,
        is_active=True,
        registration_source="bootstrap",
        created_at=_NOW,
        is_genesis=role == "platform_admin" if is_genesis is None else is_genesis,
        identity=identity,
    )


def _identity_ns(
    *,
    email="admin@example.com",
    password=_DEFAULT_TEST_PASSWORD,
    must_change_password=True,
    is_platform_admin=True,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        username="admin",
        phone=None,
        password_hash=hash_password(password),
        is_active=True,
        email_verified=True,
        is_platform_admin=is_platform_admin,
        must_change_password=must_change_password,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _user_ns(identity, *, role="platform_admin"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        role=role,
        tenant_id=uuid.uuid4(),
        display_name="Admin",
        avatar_url=None,
        email=identity.email,
        username=identity.username,
        is_active=True,
        is_platform_admin=identity.is_platform_admin,
        must_change_password=identity.must_change_password,
        email_verified=True,
        created_at=_NOW,
        registration_source="bootstrap",
        title=None,
        primary_mobile=None,
    )


@pytest.mark.asyncio
async def test_login_returns_must_change_password(monkeypatch):
    identity = _identity_ns(must_change_password=True)
    user = _user_ns(identity)
    tenant = SimpleNamespace(id=user.tenant_id, is_active=True, name="Default")

    monkeypatch.setattr("app.dao.identity_dao.get_by_login_identifier", AsyncMock(return_value=identity))
    monkeypatch.setattr("app.dao.user_dao.get_by_identity_id", AsyncMock(return_value=[user]))
    monkeypatch.setattr("app.dao.tenant_dao.get", AsyncMock(return_value=tenant))

    data = SimpleNamespace(
        login_identifier=identity.email,
        password="initial-password",
        tenant_id=None,
    )
    result = await auth_api.login(data, AsyncMock())
    assert result.must_change_password is True
    assert result.user.must_change_password is True
    assert result.access_token


@pytest.mark.asyncio
async def test_get_current_user_blocks_when_must_change_password(monkeypatch):
    identity = _identity_record(must_change_password=True)
    user = _user_record(identity)
    token = security_mod.create_access_token(str(user.id), user.role)

    monkeypatch.setattr("app.dao.user_dao.get_with_identity", AsyncMock(return_value=user))

    credentials = SimpleNamespace(credentials=token)
    with pytest.raises(HTTPException) as exc:
        await security_mod.get_current_user(credentials)  # type: ignore[arg-type]
    assert exc.value.status_code == 403
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["must_change_password"] is True


@pytest.mark.asyncio
async def test_get_authenticated_user_allows_must_change_password(monkeypatch):
    identity = _identity_record(must_change_password=True)
    user = _user_record(identity)
    token = security_mod.create_access_token(str(user.id), user.role)

    monkeypatch.setattr("app.dao.user_dao.get_with_identity", AsyncMock(return_value=user))

    credentials = SimpleNamespace(credentials=token)
    got = await security_mod.get_authenticated_user(credentials)  # type: ignore[arg-type]
    assert got.id == user.id


@pytest.mark.asyncio
async def test_load_user_from_access_token_enforces_password_change(monkeypatch):
    identity = _identity_record(must_change_password=True)
    user = _user_record(identity)
    token = security_mod.create_access_token(str(user.id), user.role)
    monkeypatch.setattr("app.dao.user_dao.get_with_identity", AsyncMock(return_value=user))

    with pytest.raises(HTTPException) as exc:
        await security_mod.load_user_from_access_token(token, enforce_password_change=True)
    assert exc.value.status_code == 403
    assert exc.value.detail["must_change_password"] is True


@pytest.mark.asyncio
async def test_change_password_clears_must_change_flag(monkeypatch):
    identity = _identity_ns(must_change_password=True, password="old-password")
    user = _user_ns(identity)
    updated = []

    async def fake_update(*, db_obj, obj_in):
        updated.append(obj_in)
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj

    monkeypatch.setattr("app.dao.user_dao.get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr("app.dao.identity_dao.get", AsyncMock(return_value=identity))
    monkeypatch.setattr("app.dao.identity_dao.update", fake_update)

    data = SimpleNamespace(old_password="old-password", new_password="new-secure-password")
    result = await auth_api.change_password(data, current_user=user)
    assert result["ok"] is True
    assert result["must_change_password"] is False
    assert updated[0]["must_change_password"] is False


@pytest.mark.asyncio
async def test_change_password_rejects_same_password(monkeypatch):
    identity = _identity_ns(must_change_password=True, password="same-password")
    user = _user_ns(identity)
    data = SimpleNamespace(old_password="same-password", new_password="same-password")
    with pytest.raises(HTTPException) as exc:
        await auth_api.change_password(data, current_user=user)
    assert exc.value.status_code == 400
    assert "different" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_reset_password_clears_must_change_flag(monkeypatch):
    identity = _identity_ns(must_change_password=True)
    updated = []

    async def fake_update(*, db_obj, obj_in):
        updated.append(obj_in)
        return db_obj

    monkeypatch.setattr(
        "app.services.password_reset_service.consume_password_reset_token",
        AsyncMock(return_value={"identity_id": identity.id}),
    )
    monkeypatch.setattr("app.dao.identity_dao.get", AsyncMock(return_value=identity))
    monkeypatch.setattr("app.dao.identity_dao.update", fake_update)

    data = SimpleNamespace(token="a" * 32, new_password="brand-new-password")
    result = await auth_api.reset_password(data)
    assert result["ok"] is True
    assert updated[0]["must_change_password"] is False
    assert "password_hash" in updated[0]


@pytest.mark.asyncio
async def test_create_company_provisions_genesis_org_admin(monkeypatch):
    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(
        id=tenant_id,
        name="Acme",
        slug="acme-abc",
        is_active=True,
        created_at=None,
    )
    provisioned = SimpleNamespace(
        tenant=tenant,
        org_admin=SimpleNamespace(id=uuid.uuid4(), role="org_admin"),
        admin_email="orgadmin@acme.com",
    )
    platform_user = SimpleNamespace(id=uuid.uuid4(), role="platform_admin", tenant_id=None)
    monkeypatch.setattr("app.api.admin.create_tenant_with_org_admin", AsyncMock(return_value=provisioned))

    result = await admin_api.create_company(
        admin_api.CompanyCreateRequest(
            name="Acme",
            admin_email="orgadmin@acme.com",
            admin_password="temp-password",
            admin_display_name="Org Admin",
        ),
        current_user=platform_user,
    )

    assert result.org_admin_email == "orgadmin@acme.com"
    assert result.must_change_password is True
    assert result.company.id == tenant_id
    assert result.company.user_count == 1


@pytest.mark.asyncio
async def test_create_company_rejects_claimed_admin_domain(monkeypatch):
    from app.services.org_membership import DomainClaimedError

    monkeypatch.setattr(
        "app.api.admin.create_tenant_with_org_admin",
        AsyncMock(side_effect=DomainClaimedError("Email domain is already claimed")),
    )
    platform_user = SimpleNamespace(id=uuid.uuid4(), role="platform_admin")

    with pytest.raises(HTTPException) as exc:
        await admin_api.create_company(
            admin_api.CompanyCreateRequest(
                name="Marathon",
                admin_email="techadmin@marathon.vn",
                admin_password="temp-password",
            ),
            current_user=platform_user,
        )
    assert exc.value.status_code == 409
    assert "domain" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_create_company_rejects_existing_admin_email(monkeypatch):
    from app.services.tenant_provisioning import AdminEmailTakenError

    monkeypatch.setattr(
        "app.api.admin.create_tenant_with_org_admin",
        AsyncMock(side_effect=AdminEmailTakenError("taken@example.com")),
    )
    platform_user = SimpleNamespace(id=uuid.uuid4(), role="platform_admin")

    with pytest.raises(HTTPException) as exc:
        await admin_api.create_company(
            admin_api.CompanyCreateRequest(
                name="Acme",
                admin_email="taken@example.com",
                admin_password="temp-password",
            ),
            current_user=platform_user,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_ensure_platform_admin_creates_when_missing(monkeypatch):
    from app.services import platform_admin_seeder as seeder

    settings = SimpleNamespace(
        PLATFORM_ADMIN_EMAIL="platform@example.com",
        PLATFORM_ADMIN_PASSWORD="bootstrap-secret",
    )
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        email="platform@example.com",
        username="platform",
        is_platform_admin=True,
        is_active=True,
        email_verified=True,
        must_change_password=True,
    )
    user = SimpleNamespace(
        id=uuid.uuid4(),
        role="platform_admin",
        identity=identity,
        is_active=True,
        display_name="platform",
        avatar_url=None,
        tenant_id=None,
    )

    monkeypatch.setattr(seeder, "get_settings", lambda: settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_platform_admin", AsyncMock(return_value=None))
    monkeypatch.setattr(seeder.user_dao, "first_by_role", AsyncMock(return_value=None))
    monkeypatch.setattr(seeder.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(seeder.identity_dao, "is_username_taken", AsyncMock(return_value=False))
    monkeypatch.setattr(seeder, "hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(seeder.identity_dao, "create_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(seeder.user_dao, "get_by_identity_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(seeder.user_dao, "create", AsyncMock(return_value=user))
    monkeypatch.setattr(seeder.participant_dao, "create_for_user", AsyncMock())
    maraclaw = _patch_maraclaw(monkeypatch, seeder)

    with patch.object(seeder, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await seeder.ensure_platform_admin()

    assert result is user
    kwargs = seeder.identity_dao.create_identity.await_args.kwargs
    assert kwargs["email"] == "platform@example.com"
    assert kwargs["is_platform_admin"] is True
    assert kwargs["must_change_password"] is True
    create_user_kwargs = seeder.user_dao.create.await_args.kwargs["obj_in"]
    assert create_user_kwargs["tenant_id"] == maraclaw.id
    assert create_user_kwargs["role"] == "platform_admin"
    assert create_user_kwargs["is_genesis"] is True


@pytest.mark.asyncio
async def test_ensure_platform_admin_fails_without_env_when_missing(monkeypatch):
    from app.services import platform_admin_seeder as seeder

    settings = SimpleNamespace(PLATFORM_ADMIN_EMAIL="", PLATFORM_ADMIN_PASSWORD="")
    monkeypatch.setattr(seeder, "get_settings", lambda: settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_platform_admin", AsyncMock(return_value=None))
    monkeypatch.setattr(seeder.user_dao, "first_by_role", AsyncMock(return_value=None))

    with pytest.raises(seeder.PlatformAdminSeedError, match="not found in the database"):
        await seeder.ensure_platform_admin()


@pytest.mark.asyncio
async def test_ensure_platform_admin_existing_email_requires_password(monkeypatch):
    from app.services import platform_admin_seeder as seeder

    settings = SimpleNamespace(
        PLATFORM_ADMIN_EMAIL="platform@example.com",
        PLATFORM_ADMIN_PASSWORD="bootstrap-secret",
    )
    identity = _identity_record(
        email="platform@example.com",
        password="other-password",
        is_platform_admin=False,
        must_change_password=False,
    )
    monkeypatch.setattr(seeder, "get_settings", lambda: settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_platform_admin", AsyncMock(return_value=None))
    monkeypatch.setattr(seeder.user_dao, "first_by_role", AsyncMock(return_value=None))
    monkeypatch.setattr(seeder.identity_dao, "get_by_email", AsyncMock(return_value=identity))

    with patch.object(seeder, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        with pytest.raises(seeder.PlatformAdminSeedError) as exc:
            await seeder.ensure_platform_admin()
    assert "does not match" in str(exc.value)


@pytest.mark.asyncio
async def test_ensure_platform_admin_elevates_when_password_matches(monkeypatch):
    from app.services import platform_admin_seeder as seeder

    settings = SimpleNamespace(
        PLATFORM_ADMIN_EMAIL="platform@example.com",
        PLATFORM_ADMIN_PASSWORD="bootstrap-secret",
    )
    identity = _identity_record(
        email="platform@example.com",
        password="bootstrap-secret",
        is_platform_admin=False,
        must_change_password=False,
    )
    user = _user_record(identity, role="member", tenant_id=None)
    updates = []

    async def fake_identity_update(*, db_obj, obj_in):
        updates.append(obj_in)
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj

    async def fake_user_update(*, db_obj, obj_in):
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj

    monkeypatch.setattr(seeder, "get_settings", lambda: settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_platform_admin", AsyncMock(return_value=None))
    monkeypatch.setattr(seeder.user_dao, "first_by_role", AsyncMock(return_value=None))
    monkeypatch.setattr(seeder.identity_dao, "get_by_email", AsyncMock(return_value=identity))
    monkeypatch.setattr(seeder.identity_dao, "update", fake_identity_update)
    monkeypatch.setattr(seeder.user_dao, "get_by_identity_id", AsyncMock(return_value=[user]))
    monkeypatch.setattr(seeder.user_dao, "update", fake_user_update)
    monkeypatch.setattr(seeder.identity_dao, "create_identity", AsyncMock())
    _patch_maraclaw(monkeypatch, seeder)

    with patch.object(seeder, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await seeder.ensure_platform_admin()

    assert result.role == "platform_admin"
    assert updates
    assert updates[0]["is_platform_admin"] is True
    assert updates[0]["must_change_password"] is True
    seeder.identity_dao.create_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_identifier_lookup_is_email_case_insensitive():
    import inspect

    from app.dao.identity_dao import IdentityDAO

    source = inspect.getsource(IdentityDAO.get_by_login_identifier)
    assert "lower(email)" in source


@pytest.mark.asyncio
async def test_ensure_platform_admin_skips_when_admin_exists(monkeypatch):
    from app.services import platform_admin_seeder as seeder

    settings = SimpleNamespace(PLATFORM_ADMIN_EMAIL="x@y.com", PLATFORM_ADMIN_PASSWORD="secret")
    identity = _identity_record()
    admin = _user_record(identity)
    warned = []
    monkeypatch.setattr(seeder, "get_settings", lambda: settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_platform_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(seeder.user_dao, "get_with_identity", AsyncMock(return_value=admin))
    monkeypatch.setattr(seeder.identity_dao, "get_by_email", AsyncMock())
    monkeypatch.setattr(seeder.user_dao, "get_by_identity_id", AsyncMock(return_value=[admin]))

    async def fake_update(*, db_obj, obj_in):
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj

    monkeypatch.setattr(seeder.user_dao, "update", fake_update)
    monkeypatch.setattr(seeder.logger, "warning", lambda *args, **kwargs: warned.append(args))
    _patch_maraclaw(monkeypatch, seeder)

    result = await seeder.ensure_platform_admin()
    assert result is admin
    assert result.tenant_id == _MARACLAW_ID
    seeder.identity_dao.get_by_email.assert_not_awaited()
    assert warned
    assert "does not match genesis platform admin" in warned[0][0]


@pytest.mark.asyncio
async def test_ensure_platform_admin_uses_db_credentials_without_env(monkeypatch):
    from app.services import platform_admin_seeder as seeder

    settings = SimpleNamespace(PLATFORM_ADMIN_EMAIL="", PLATFORM_ADMIN_PASSWORD="")
    identity = _identity_record()
    admin = _user_record(identity)
    monkeypatch.setattr(seeder, "get_settings", lambda: settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_platform_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(seeder.user_dao, "get_with_identity", AsyncMock(return_value=admin))
    monkeypatch.setattr(seeder.user_dao, "get_by_identity_id", AsyncMock(return_value=[admin]))

    async def fake_update(*, db_obj, obj_in):
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj

    monkeypatch.setattr(seeder.user_dao, "update", fake_update)
    _patch_maraclaw(monkeypatch, seeder)

    result = await seeder.ensure_platform_admin()
    assert result is admin
    assert result.tenant_id == _MARACLAW_ID


@pytest.mark.asyncio
async def test_ensure_platform_admin_fails_when_db_lacks_credentials_and_env_missing(monkeypatch):
    from app.services import platform_admin_seeder as seeder

    settings = SimpleNamespace(PLATFORM_ADMIN_EMAIL="", PLATFORM_ADMIN_PASSWORD="")
    identity = _identity_record()
    identity.password_hash = None
    admin = _user_record(identity)
    monkeypatch.setattr(seeder, "get_settings", lambda: settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_platform_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(seeder.user_dao, "get_with_identity", AsyncMock(return_value=admin))

    with pytest.raises(seeder.PlatformAdminSeedError, match="not found in the database"):
        await seeder.ensure_platform_admin()


@pytest.mark.asyncio
async def test_ensure_platform_admin_repairs_missing_password_from_env(monkeypatch):
    from app.services import platform_admin_seeder as seeder

    settings = SimpleNamespace(
        PLATFORM_ADMIN_EMAIL="platform@example.com",
        PLATFORM_ADMIN_PASSWORD="bootstrap-secret",
    )
    identity = _identity_record(email="platform@example.com", password="bootstrap-secret")
    identity.password_hash = None
    admin = _user_record(identity)
    updates = []

    async def fake_identity_update(*, db_obj, obj_in):
        updates.append(obj_in)
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj

    monkeypatch.setattr(seeder, "get_settings", lambda: settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_platform_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(seeder.user_dao, "get_with_identity", AsyncMock(return_value=admin))
    monkeypatch.setattr(seeder, "hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(seeder.identity_dao, "update", fake_identity_update)
    monkeypatch.setattr(seeder.user_dao, "get_by_identity_id", AsyncMock(return_value=[admin]))
    monkeypatch.setattr(seeder.user_dao, "update", AsyncMock(return_value=admin))
    _patch_maraclaw(monkeypatch, seeder)

    with patch.object(seeder, "connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await seeder.ensure_platform_admin()

    assert result.role == "platform_admin"
    assert updates[0]["password_hash"] == "hashed"
    assert updates[0]["must_change_password"] is True


@pytest.mark.asyncio
async def test_register_init_never_elevates_to_platform_admin(monkeypatch):
    captured = {}

    async def fake_find_or_create_identity(**kwargs):
        captured["identity_kwargs"] = kwargs
        return SimpleNamespace(
            id=uuid.uuid4(),
            email=kwargs["email"],
            username=kwargs.get("username") or "u",
            email_verified=True,
            is_platform_admin=False,
            must_change_password=False,
        )

    async def fake_create_user(**kwargs):
        captured["user_kwargs"] = kwargs
        identity = kwargs["identity"]
        return SimpleNamespace(
            id=uuid.uuid4(),
            identity_id=identity.id,
            identity=identity,
            tenant_id=None,
            role=kwargs["role"],
            email=identity.email,
            display_name=kwargs["display_name"],
            is_active=True,
            is_platform_admin=False,
            must_change_password=False,
            email_verified=True,
            created_at=_NOW,
            avatar_url=None,
            title=None,
            primary_mobile=None,
            registration_source="web",
            username=identity.username,
        )

    monkeypatch.setattr("app.dao.identity_dao.get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr("app.dao.user_dao.get_by_identity_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.services.org_membership.place_new_registration",
        AsyncMock(return_value=SimpleNamespace(tenant_id=None, suggested=None, needs_org_confirm=False)),
    )
    monkeypatch.setattr(
        "app.services.system_email_service.resolve_email_config_async",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(auth_api, "hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(auth_api, "create_access_token", lambda *a, **k: "token")
    monkeypatch.setattr(auth_api, "_send_verification_email_task", AsyncMock())

    with (
        patch("app.api.auth.connection_ctx") as ctx,
        patch(
            "app.services.registration_service.registration_service.find_or_create_identity",
            fake_find_or_create_identity,
        ),
        patch("app.services.registration_service.registration_service.create_user_with_identity", fake_create_user),
    ):
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)

        data = SimpleNamespace(
            email="new@example.com",
            username="newuser",
            password="password123",
            display_name="New User",
            target_tenant_id=None,
        )
        result = await auth_api.register_init(data, AsyncMock())

    assert captured["identity_kwargs"]["is_platform_admin"] is False
    assert captured["user_kwargs"]["role"] == "member"
    assert result.user.role == "member"


@pytest.mark.asyncio
async def test_raise_if_password_change_required_blocks_tenant_ops():
    identity = _identity_record(must_change_password=True)
    user = _user_record(identity)
    with pytest.raises(HTTPException) as exc:
        security_mod.raise_if_password_change_required(user)
    assert exc.value.status_code == 403
