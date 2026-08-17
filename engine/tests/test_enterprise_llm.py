"""LLM pool is org-admin configuration, not an end-user setting."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import enterprise as enterprise_api
from app.records.llm import LLMModelRecord
from app.schemas.schemas import LLMModelCreate, LLMModelUpdate
from app.services import enterprise_llm as pool

_NOW = datetime.now(UTC)
_TENANT = uuid.uuid4()
_OTHER = uuid.uuid4()


def _user(*, role="org_admin", tenant_id: uuid.UUID | None = _TENANT, identity=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        tenant_id=tenant_id,
        identity=identity,
    )


def _model(**kwargs) -> LLMModelRecord:
    defaults = {
        "id": uuid.uuid4(),
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "api_key_encrypted": "enc-secret-key-9999",
        "label": "Claude",
        "tenant_id": _TENANT,
        "base_url": "https://api.anthropic.com",
        "max_tokens_per_day": 1000,
        "enabled": True,
        "supports_vision": False,
        "temperature": 0.2,
        "request_timeout": 30,
        "max_output_tokens": 4096,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(kwargs)
    return LLMModelRecord(**defaults)


def test_member_is_not_pool_admin():
    with pytest.raises(HTTPException) as exc:
        pool.assert_llm_pool_admin(_user(role="member"))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        pool.assert_llm_pool_admin(_user(role="agent_admin"))
    pool.assert_llm_pool_admin(_user(role="org_admin"))
    pool.assert_llm_pool_admin(_user(role="platform_admin"))
    pool.assert_llm_pool_admin(_user(role="member", identity=SimpleNamespace(is_platform_admin=True)))


def test_org_admin_cannot_target_another_tenant():
    with pytest.raises(HTTPException) as exc:
        pool.resolve_llm_pool_tenant_id(_user(role="org_admin"), str(_OTHER))
    assert exc.value.status_code == 403
    assert pool.resolve_llm_pool_tenant_id(_user(role="org_admin"), str(_TENANT)) == _TENANT
    assert pool.resolve_llm_pool_tenant_id(_user(role="org_admin"), None) == _TENANT
    assert pool.resolve_llm_pool_tenant_id(_user(role="member", tenant_id=None), None) is None


def test_org_admin_cannot_manage_foreign_or_global_model():
    own = _model()
    pool.assert_can_manage_model(_user(role="org_admin"), own)
    with pytest.raises(HTTPException) as exc:
        pool.assert_can_manage_model(_user(role="org_admin"), _model(tenant_id=_OTHER))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        pool.assert_can_manage_model(_user(role="org_admin"), _model(tenant_id=None))
    pool.assert_can_manage_model(_user(role="platform_admin", tenant_id=None), _model(tenant_id=_OTHER))


def test_model_usable_in_tenant_is_org_only():
    own = _model()
    assert pool.model_usable_in_tenant(own, _TENANT) is True
    assert pool.model_usable_in_tenant(own, _OTHER) is False
    assert pool.model_usable_in_tenant(own, None) is False
    assert pool.model_usable_in_tenant(_model(tenant_id=None), _TENANT) is False
    assert pool.model_usable_in_tenant(_model(enabled=False), _TENANT) is False
    assert pool.owned_model_or_none(_model(tenant_id=_OTHER), _TENANT) is None
    assert pool.owned_model_or_none(own, _TENANT) is own


def test_member_serialization_strips_secrets():
    model = _model()
    public = pool.serialize_llm_model(model, is_admin=False, default_model_id=model.id)
    assert public.api_key_masked == ""
    assert public.base_url is None
    assert public.request_timeout is None
    assert public.max_tokens_per_day is None
    assert public.is_default is True
    assert public.is_fallback is False
    assert public.provider == "anthropic"

    fallback = pool.serialize_llm_model(model, is_admin=False, default_model_id=None, fallback_model_id=model.id)
    assert fallback.is_fallback is True
    assert fallback.is_default is False
    assert fallback.is_secondary is False

    secondary = pool.serialize_llm_model(model, is_admin=False, default_model_id=None, secondary_model_id=model.id)
    assert secondary.is_secondary is True
    assert secondary.is_default is False
    assert secondary.is_fallback is False

    with patch.object(pool, "get_model_api_key", return_value="sk-live-9999"):
        admin = pool.serialize_llm_model(model, is_admin=True, default_model_id=None)
    assert admin.api_key_masked == "****9999"
    assert admin.base_url == "https://api.anthropic.com"
    assert admin.is_default is False


@pytest.mark.asyncio
async def test_assert_models_in_tenant_pool_rejects_foreign_and_disabled():
    foreign = _model(tenant_id=_OTHER)
    disabled = _model(enabled=False)
    ok = _model()
    with patch.object(pool.llm_model_dao, "get_many", AsyncMock(return_value=[foreign])):
        with pytest.raises(HTTPException) as exc:
            await pool.assert_models_in_tenant_pool(_TENANT, foreign.id)
        assert exc.value.status_code == 400
    with (
        patch.object(pool.llm_model_dao, "get_many", AsyncMock(return_value=[disabled])),
        pytest.raises(HTTPException),
    ):
        await pool.assert_models_in_tenant_pool(_TENANT, disabled.id)
    with patch.object(pool.llm_model_dao, "get_many", AsyncMock(return_value=[ok])):
        await pool.assert_models_in_tenant_pool(_TENANT, ok.id)
    await pool.assert_models_in_tenant_pool(_TENANT, None)
    global_row = _model(tenant_id=None)
    with (
        patch.object(pool.llm_model_dao, "get_many", AsyncMock(return_value=[global_row])),
        pytest.raises(HTTPException) as global_denied,
    ):
        await pool.assert_models_in_tenant_pool(_TENANT, global_row.id)
    assert global_denied.value.status_code == 400


@pytest.mark.asyncio
async def test_list_providers_rejects_members():
    with pytest.raises(HTTPException) as exc:
        await enterprise_api.list_llm_providers(current_user=_user(role="member"))
    assert exc.value.status_code == 403
    result = await enterprise_api.list_llm_providers(current_user=_user(role="org_admin"))
    assert isinstance(result, list)
    assert any(item["provider"] == "anthropic" for item in result)
    grok = next(item for item in result if item["provider"] == "grok")
    assert grok["display_name"] == "Grok (xAI)"
    assert grok["default_base_url"] == "https://api.x.ai/v1"
    assert grok["default_model"] == "grok-4.6"


@pytest.mark.asyncio
async def test_list_models_member_redacts_and_hides_disabled(monkeypatch):
    enabled = _model()
    disabled = _model(enabled=False, label="Hidden")
    tenant = SimpleNamespace(id=_TENANT, default_model_id=enabled.id)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "list_for_tenant", AsyncMock(return_value=[enabled, disabled]))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))

    rows = await enterprise_api.list_llm_models(current_user=_user(role="member"))
    assert len(rows) == 1
    assert rows[0].id == enabled.id
    assert rows[0].api_key_masked == ""
    assert rows[0].base_url is None
    assert rows[0].is_default is True

    admin_rows = await enterprise_api.list_llm_models(current_user=_user(role="org_admin"))
    assert {row.label for row in admin_rows} == {"Claude", "Hidden"}
    assert admin_rows[0].api_key_masked.startswith("****") or admin_rows[1].api_key_masked.startswith("****")


@pytest.mark.asyncio
async def test_list_models_member_without_tenant_is_empty():
    rows = await enterprise_api.list_llm_models(current_user=_user(role="member", tenant_id=None))
    assert rows == []


@pytest.mark.asyncio
async def test_add_model_org_admin_own_tenant_only(monkeypatch):
    created = _model()
    monkeypatch.setattr(enterprise_api, "encrypt_data", lambda value, _key: f"enc:{value}")
    monkeypatch.setattr(enterprise_api.llm_model_dao, "create", AsyncMock(return_value=created))
    monkeypatch.setattr(
        enterprise_api.tenant_dao,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=_TENANT, default_model_id=None)),
    )
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", AsyncMock())

    data = LLMModelCreate(
        provider="anthropic",
        model="claude-sonnet-4-5",
        api_key="sk-test",
        label="Claude",
    )
    with pytest.raises(HTTPException) as denied:
        await enterprise_api.add_llm_model(data, tenant_id=str(_OTHER), current_user=_user(role="org_admin"))
    assert denied.value.status_code == 403

    with pytest.raises(HTTPException) as member_denied:
        await enterprise_api.add_llm_model(data, current_user=_user(role="member"))
    assert member_denied.value.status_code == 403

    with pytest.raises(HTTPException) as pa_needs_company:
        await enterprise_api.add_llm_model(data, current_user=_user(role="platform_admin", tenant_id=_TENANT))
    assert pa_needs_company.value.status_code == 400

    out = await enterprise_api.add_llm_model(data, current_user=_user(role="org_admin"))
    assert out.id == created.id
    create_kwargs = enterprise_api.llm_model_dao.create.await_args.kwargs["obj_in"]
    assert create_kwargs["tenant_id"] == _TENANT


@pytest.mark.asyncio
async def test_update_and_delete_reject_other_tenant(monkeypatch):
    foreign = _model(tenant_id=_OTHER)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=foreign))
    with pytest.raises(HTTPException) as exc:
        await enterprise_api.update_llm_model(
            foreign.id, LLMModelUpdate(label="Nope"), current_user=_user(role="org_admin")
        )
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        await enterprise_api.remove_llm_model(foreign.id, current_user=_user(role="org_admin"))
    with pytest.raises(HTTPException):
        await enterprise_api.set_default_llm_model(foreign.id, current_user=_user(role="org_admin"))
    with pytest.raises(HTTPException):
        await enterprise_api.set_fallback_llm_model(foreign.id, current_user=_user(role="org_admin"))
    with pytest.raises(HTTPException):
        await enterprise_api.set_secondary_llm_model(foreign.id, current_user=_user(role="org_admin"))


@pytest.mark.asyncio
async def test_set_fallback_rejects_primary_and_sets_own(monkeypatch):
    primary = _model()
    fallback = _model(label="Haiku")
    tenant = SimpleNamespace(
        id=_TENANT,
        default_model_id=primary.id,
        default_fallback_model_id=None,
    )
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=primary))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    with pytest.raises(HTTPException) as same:
        await enterprise_api.set_fallback_llm_model(primary.id, current_user=_user(role="org_admin"))
    assert same.value.status_code == 400

    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=fallback))
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", AsyncMock())
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_fallback_model", AsyncMock(return_value=2))
    monkeypatch.setattr(enterprise_api.agent_dao, "clear_other_slots_matching", AsyncMock())
    await enterprise_api.set_fallback_llm_model(fallback.id, current_user=_user(role="org_admin"))
    update_kwargs = enterprise_api.tenant_dao.update.await_args.kwargs["obj_in"]
    assert update_kwargs["default_fallback_model_id"] == fallback.id


@pytest.mark.asyncio
async def test_set_secondary_rejects_primary_and_fallback(monkeypatch):
    primary = _model()
    fallback = _model(label="Haiku")
    secondary = _model(label="Flash")
    tenant = SimpleNamespace(
        id=_TENANT,
        default_model_id=primary.id,
        default_fallback_model_id=fallback.id,
        default_secondary_model_id=None,
    )
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=primary))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    with pytest.raises(HTTPException) as same:
        await enterprise_api.set_secondary_llm_model(primary.id, current_user=_user(role="org_admin"))
    assert same.value.status_code == 400

    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=fallback))
    with pytest.raises(HTTPException) as vs_fallback:
        await enterprise_api.set_secondary_llm_model(fallback.id, current_user=_user(role="org_admin"))
    assert vs_fallback.value.status_code == 400

    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=secondary))
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", AsyncMock())
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_secondary_model", AsyncMock(return_value=3))
    monkeypatch.setattr(enterprise_api.agent_dao, "clear_other_slots_matching", AsyncMock())
    await enterprise_api.set_secondary_llm_model(secondary.id, current_user=_user(role="org_admin"))
    update_kwargs = enterprise_api.tenant_dao.update.await_args.kwargs["obj_in"]
    assert update_kwargs["default_secondary_model_id"] == secondary.id


@pytest.mark.asyncio
async def test_set_default_clears_secondary_collision(monkeypatch):
    secondary = _model(label="Flash")
    tenant = SimpleNamespace(
        id=_TENANT,
        default_model_id=uuid.uuid4(),
        default_fallback_model_id=None,
        default_secondary_model_id=secondary.id,
    )
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=secondary))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", AsyncMock())
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_primary_model", AsyncMock(return_value=1))
    clear = AsyncMock()
    monkeypatch.setattr(enterprise_api.agent_dao, "clear_other_slots_matching", clear)
    await enterprise_api.set_default_llm_model(secondary.id, current_user=_user(role="org_admin"))
    update_kwargs = enterprise_api.tenant_dao.update.await_args.kwargs["obj_in"]
    assert update_kwargs["default_model_id"] == secondary.id
    assert update_kwargs["default_secondary_model_id"] is None
    clear.assert_awaited_once()
    assert clear.await_args.kwargs["keep"] == "primary"


@pytest.mark.asyncio
async def test_set_fallback_rejects_current_secondary(monkeypatch):
    secondary = _model(label="Flash")
    tenant = SimpleNamespace(
        id=_TENANT,
        default_model_id=uuid.uuid4(),
        default_fallback_model_id=None,
        default_secondary_model_id=secondary.id,
    )
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=secondary))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    with pytest.raises(HTTPException) as exc:
        await enterprise_api.set_fallback_llm_model(secondary.id, current_user=_user(role="org_admin"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_disable_model_clears_tenant_secondary(monkeypatch):
    secondary = _model(label="Flash")
    tenant = SimpleNamespace(
        id=_TENANT,
        default_model_id=uuid.uuid4(),
        default_fallback_model_id=None,
        default_secondary_model_id=secondary.id,
    )
    updated = _model(id=secondary.id, label="Flash", enabled=False)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=secondary))
    monkeypatch.setattr(enterprise_api.llm_model_dao, "update", AsyncMock(return_value=updated))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    tenant_update = AsyncMock(
        return_value=SimpleNamespace(
            id=_TENANT,
            default_model_id=tenant.default_model_id,
            default_fallback_model_id=None,
            default_secondary_model_id=None,
        )
    )
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", tenant_update)
    out = await enterprise_api.update_llm_model(
        secondary.id, LLMModelUpdate(enabled=False), current_user=_user(role="org_admin")
    )
    assert tenant_update.await_args.kwargs["obj_in"]["default_secondary_model_id"] is None
    assert out.is_secondary is False


def test_assert_distinct_model_slots():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pool.assert_distinct_model_slots(a, b, c)
    pool.assert_distinct_model_slots(a, None, None)
    with pytest.raises(HTTPException) as exc:
        pool.assert_distinct_model_slots(a, a, None)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_probe_rejects_foreign_saved_model(monkeypatch):
    foreign = _model(tenant_id=_OTHER)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=foreign))
    with pytest.raises(HTTPException) as exc:
        await enterprise_api.probe_llm_model(
            enterprise_api.LLMTestRequest(
                provider="anthropic",
                model="claude-sonnet-4-5",
                model_id=str(foreign.id),
            ),
            current_user=_user(role="org_admin"),
        )
    assert exc.value.status_code == 403
