from uuid import uuid4

import pytest

from app.core.json_types import JsonObject
from app.services import feishu_service as feishu_service_module
from app.services.feishu_service import FeishuOAuthUser


class _FakeResponse:
    def __init__(self, status_code: int, payload: JsonObject):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> JsonObject:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *, send_payload: JsonObject | None = None, patch_payload: JsonObject | None = None):
        self._send_payload = send_payload or {"code": 0, "msg": "ok", "data": {"message_id": "m_1"}}
        self._patch_payload = patch_payload or {"code": 0, "msg": "ok"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **_kwargs):
        if "app_access_token/internal" in url:
            return _FakeResponse(200, {"app_access_token": "token_x"})
        return _FakeResponse(200, self._send_payload)

    async def patch(self, _url, **_kwargs):
        return _FakeResponse(200, self._patch_payload)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


@pytest.mark.asyncio
async def test_send_message_raises_when_business_code_nonzero(monkeypatch):
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(send_payload={"code": 99991663, "msg": "rate limited"}),
    )

    with pytest.raises(RuntimeError, match="code=99991663"):
        await feishu_service_module.feishu_service.send_message(
            "app_id",
            "app_secret",
            "ou_xxx",
            "text",
            '{"text":"hello"}',
            stage="unit_test_send",
        )


@pytest.mark.asyncio
async def test_patch_message_raises_when_business_code_nonzero(monkeypatch):
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(patch_payload={"code": 10019, "msg": "invalid card content"}),
    )

    with pytest.raises(RuntimeError, match="code=10019"):
        await feishu_service_module.feishu_service.patch_message(
            "app_id",
            "app_secret",
            "om_xxx",
            '{"content":"test"}',
            stage="unit_test_patch",
        )


@pytest.mark.asyncio
async def test_login_or_register_links_existing_user_to_feishu_member_without_legacy_user_fields(monkeypatch):
    from types import SimpleNamespace

    tenant_id = uuid4()
    identity_id = uuid4()
    user_id = uuid4()
    provider_id = uuid4()
    identity = SimpleNamespace(id=identity_id, email="old@feishu.local")
    user = SimpleNamespace(
        id=user_id,
        identity_id=identity_id,
        identity=identity,
        tenant_id=tenant_id,
        display_name="Old Alice",
        role="member",
        email="old@feishu.local",
        avatar_url=None,
    )
    provider = SimpleNamespace(
        id=provider_id,
        provider_type="feishu",
        name="Feishu",
        config={},
        tenant_id=tenant_id,
    )
    member = SimpleNamespace(
        id=uuid4(),
        open_id="ou_open_123",
        external_id="u_emp_789",
        provider_id=provider_id,
        status="active",
        name="Alice Feishu",
        user_id=None,
    )
    member_updates: list[dict] = []
    user_updates: list[dict] = []
    identity_updates: list[dict] = []

    async def get_provider(_ptype, _tenant):
        return provider

    async def find_member(**kwargs):
        return member

    async def get_user_with_identity(_id):
        return user

    async def get_by_email_and_tenant(email, tenant):
        assert email == "alice@example.com"
        return user

    async def update_user(*, db_obj, obj_in):
        user_updates.append(obj_in)
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj

    async def update_identity(*, db_obj, obj_in):
        identity_updates.append(obj_in)
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj

    async def update_member(*, db_obj, obj_in):
        member_updates.append(obj_in)
        for k, v in obj_in.items():
            setattr(db_obj, k, v)
        return db_obj

    async def get_identity(_id):
        return identity

    monkeypatch.setattr(feishu_service_module.identity_provider_dao, "get_by_type_and_tenant", get_provider)
    monkeypatch.setattr(feishu_service_module.org_member_dao, "find_active_by_any_ids", find_member)
    monkeypatch.setattr(feishu_service_module.user_dao, "get_with_identity", get_user_with_identity)
    monkeypatch.setattr(feishu_service_module.user_dao, "get_by_email_and_tenant", get_by_email_and_tenant)
    monkeypatch.setattr(feishu_service_module.user_dao, "update", update_user)
    monkeypatch.setattr(feishu_service_module.identity_dao, "get", get_identity)
    monkeypatch.setattr(feishu_service_module.identity_dao, "update", update_identity)
    monkeypatch.setattr(feishu_service_module.org_member_dao, "update", update_member)
    monkeypatch.setattr(feishu_service_module, "create_access_token", lambda _user_id, _role: "token")

    feishu_user: FeishuOAuthUser = {
        "open_id": "ou_open_123",
        "union_id": "on_union_456",
        "user_id": "u_emp_789",
        "name": "Alice Feishu",
        "email": "alice@example.com",
        "avatar_url": "https://example.com/alice.png",
    }

    returned_user, token = await feishu_service_module.feishu_service.login_or_register(
        None,
        feishu_user,
        str(tenant_id),
    )

    assert returned_user is user
    assert token == "token"
    assert member.user_id == user.id
    assert user.display_name == "Alice Feishu"
    assert user.avatar_url == "https://example.com/alice.png"
    assert identity.email == "alice@example.com"
    assert member_updates
    assert member_updates[-1]["user_id"] == user.id
    assert not hasattr(user, "external_id")
    assert not hasattr(user, "feishu_user_id")
