"""LLM pool is org-admin configuration, not an end-user setting."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TypedDict, Unpack
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import enterprise as enterprise_api
from app.core.domain.users import UserRole
from app.core.stored_credentials import seal_credential
from app.dao.credential_contexts import model_key_context
from app.records.identity import IdentityRecord
from app.records.llm import LLMModelRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.schemas import LLMModelCreate, LLMModelUpdate
from app.services import enterprise_llm as pool

_NOW = datetime.now(UTC)
_TENANT = uuid.uuid4()
_OTHER = uuid.uuid4()


def _user(
    *, role: str = "org_admin", tenant_id: uuid.UUID | None = _TENANT, identity: IdentityRecord | None = None
) -> UserRecord:
    return UserRecord(
        id=uuid.uuid4(),
        identity_id=identity.id if identity else None,
        role=UserRole(role),
        tenant_id=tenant_id,
        identity=identity,
    )


class _ModelOverrides(TypedDict, total=False):
    id: uuid.UUID
    provider: str
    model: str
    api_key_encrypted: str
    label: str
    tenant_id: uuid.UUID | None
    base_url: str | None
    max_tokens_per_day: int | None
    enabled: bool
    supports_vision: bool
    temperature: float | None
    request_timeout: int | None
    max_output_tokens: int | None
    reasoning_effort: str | None
    auth_kind: str
    refresh_token_encrypted: str | None
    token_expires_at: datetime | None
    oauth_account_id: str | None
    created_at: datetime | None
    updated_at: datetime | None


def _model(**overrides: Unpack[_ModelOverrides]) -> LLMModelRecord:
    model_id = overrides["id"] if "id" in overrides else uuid.uuid4()
    tenant_id = overrides.get("tenant_id", _TENANT)
    encrypted_key = (
        overrides["api_key_encrypted"]
        if "api_key_encrypted" in overrides
        else seal_credential("secret-key-9999", model_key_context(tenant_id, model_id))
    )
    return LLMModelRecord(
        id=model_id,
        provider=overrides.get("provider", "anthropic"),
        model=overrides.get("model", "claude-sonnet-4-5"),
        api_key_encrypted=encrypted_key,
        label=overrides.get("label", "Claude"),
        tenant_id=tenant_id,
        base_url=overrides.get("base_url", "https://api.anthropic.com"),
        max_tokens_per_day=overrides.get("max_tokens_per_day", 1000),
        enabled=overrides.get("enabled", True),
        supports_vision=overrides.get("supports_vision", False),
        temperature=overrides.get("temperature", 0.2),
        request_timeout=overrides.get("request_timeout", 30),
        max_output_tokens=overrides.get("max_output_tokens", 4096),
        reasoning_effort=overrides.get("reasoning_effort"),
        auth_kind=overrides.get("auth_kind", "api_key"),
        refresh_token_encrypted=overrides.get("refresh_token_encrypted"),
        token_expires_at=overrides.get("token_expires_at"),
        oauth_account_id=overrides.get("oauth_account_id"),
        created_at=overrides.get("created_at", _NOW),
        updated_at=overrides.get("updated_at", _NOW),
    )


def _tenant(
    *, default_model_id: uuid.UUID | None = None, default_fallback_model_id: uuid.UUID | None = None
) -> TenantRecord:
    return TenantRecord(
        id=_TENANT,
        name="Acme",
        slug="acme",
        tier=OrganizationPlanId.PRO,
        default_model_id=default_model_id,
        default_fallback_model_id=default_fallback_model_id,
    )


def test_member_is_not_pool_admin():
    with pytest.raises(HTTPException) as exc:
        pool.assert_llm_pool_admin(_user(role="member"))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        pool.assert_llm_pool_admin(_user(role="agent_admin"))
    pool.assert_llm_pool_admin(_user(role="org_admin"))
    pool.assert_llm_pool_admin(_user(role="platform_admin"))
    pool.assert_llm_pool_admin(_user(role="member", identity=IdentityRecord(id=uuid.uuid4(), is_platform_admin=True)))


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
    assert grok["default_model"] == "grok-4.8"


@pytest.mark.asyncio
async def test_list_models_member_redacts_and_hides_disabled(monkeypatch):
    enabled = _model()
    disabled = _model(enabled=False, label="Hidden")
    tenant = _tenant(default_model_id=enabled.id)
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

    class ModelCreateInput(TypedDict):
        tenant_id: uuid.UUID | None

    creates: list[ModelCreateInput] = []

    def record_create(*, obj_in: ModelCreateInput) -> LLMModelRecord:
        creates.append(obj_in)
        return created

    create = AsyncMock(side_effect=record_create)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "create", create)
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=_tenant()))
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", AsyncMock())

    data = LLMModelCreate(
        provider="anthropic",
        model="claude-sonnet-4-5",
        api_key="sk-test",
        label="Claude",
        temperature=None,
        reasoning_effort=None,
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
    assert create.await_args is not None
    create_kwargs = creates[-1]
    assert create_kwargs["tenant_id"] == _TENANT


@pytest.mark.asyncio
async def test_update_and_delete_reject_other_tenant(monkeypatch):
    foreign = _model(tenant_id=_OTHER)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=foreign))
    with pytest.raises(HTTPException) as exc:
        await enterprise_api.update_llm_model(
            foreign.id,
            LLMModelUpdate(label="Nope", temperature=None, reasoning_effort=None),
            current_user=_user(role="org_admin"),
        )
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        await enterprise_api.remove_llm_model(foreign.id, current_user=_user(role="org_admin"))
    with pytest.raises(HTTPException):
        await enterprise_api.set_default_llm_model(foreign.id, current_user=_user(role="org_admin"))
    with pytest.raises(HTTPException):
        await enterprise_api.set_fallback_llm_model(foreign.id, current_user=_user(role="org_admin"))


def _tenant_update_mock() -> tuple[AsyncMock, list[dict[str, uuid.UUID | None]]]:
    updates: list[dict[str, uuid.UUID | None]] = []

    def record_update(*, db_obj: TenantRecord, obj_in: dict[str, uuid.UUID | None]) -> TenantRecord:
        updates.append(obj_in)
        return db_obj

    return AsyncMock(side_effect=record_update), updates


@pytest.mark.asyncio
async def test_set_fallback_rejects_primary_and_sets_own(monkeypatch):
    primary = _model()
    fallback = _model(label="Haiku")
    tenant = _tenant(default_model_id=primary.id)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=primary))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    with pytest.raises(HTTPException) as same:
        await enterprise_api.set_fallback_llm_model(primary.id, current_user=_user(role="org_admin"))
    assert same.value.status_code == 400

    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=fallback))
    update, updates = _tenant_update_mock()
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", update)
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_fallback_model", AsyncMock(return_value=2))
    monkeypatch.setattr(enterprise_api.agent_dao, "clear_other_slots_matching", AsyncMock())
    await enterprise_api.set_fallback_llm_model(fallback.id, current_user=_user(role="org_admin"))
    assert update.await_args is not None
    update_kwargs = updates[-1]
    assert update_kwargs["default_fallback_model_id"] == fallback.id


@pytest.mark.asyncio
async def test_set_default_clears_fallback_collision(monkeypatch):
    chosen = _model(label="Flash")
    tenant = _tenant(default_model_id=uuid.uuid4(), default_fallback_model_id=chosen.id)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=chosen))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    update, updates = _tenant_update_mock()
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", update)
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_primary_model", AsyncMock(return_value=1))
    clear = AsyncMock()
    monkeypatch.setattr(enterprise_api.agent_dao, "clear_other_slots_matching", clear)
    await enterprise_api.set_default_llm_model(chosen.id, current_user=_user(role="org_admin"))
    assert update.await_args is not None
    update_kwargs = updates[-1]
    assert update_kwargs["default_model_id"] == chosen.id
    assert update_kwargs["default_fallback_model_id"] is None
    clear.assert_awaited_once()
    assert clear.await_args is not None
    assert clear.await_args.kwargs["keep"] == "primary"


@pytest.mark.asyncio
async def test_disable_model_clears_tenant_fallback(monkeypatch):
    fallback = _model(label="Flash")
    tenant = _tenant(default_model_id=uuid.uuid4(), default_fallback_model_id=fallback.id)
    updated = _model(id=fallback.id, label="Flash", enabled=False)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=fallback))
    monkeypatch.setattr(enterprise_api.llm_model_dao, "update", AsyncMock(return_value=updated))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    tenant_update = AsyncMock(return_value=replace(tenant, default_fallback_model_id=None))
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", tenant_update)
    out = await enterprise_api.update_llm_model(
        fallback.id,
        LLMModelUpdate(enabled=False, temperature=None, reasoning_effort=None),
        current_user=_user(role="org_admin"),
    )
    assert tenant_update.await_args is not None
    assert tenant_update.await_args.kwargs["obj_in"]["default_fallback_model_id"] is None
    assert out.is_fallback is False


def test_assert_distinct_model_slots():
    a, b = uuid.uuid4(), uuid.uuid4()
    pool.assert_distinct_model_slots(a, b)
    pool.assert_distinct_model_slots(a, None)
    with pytest.raises(HTTPException) as exc:
        pool.assert_distinct_model_slots(a, a)
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
