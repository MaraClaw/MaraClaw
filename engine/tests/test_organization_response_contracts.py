from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, serialize_response
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter, ValidationError
from tests.test_sso_session_status import install_session

from app.api import agent_credentials, enterprise, onboarding, sso, tenants
from app.records.agent import AgentRecord
from app.records.agent_credential import AgentCredentialRecord
from app.records.identity import IdentityProviderRecord
from app.records.onboarding import UserTenantOnboardingRecord
from app.records.system_setting import SystemSettingRecord
from app.records.user import UserRecord
from app.schemas import (
    organization_response_enterprise as enterprise_contracts,
    organization_response_onboarding as onboarding_contracts,
    organization_response_sso as sso_contracts,
    organization_response_tenant as tenant_contracts,
)
from app.services.llm.registry import ProviderManifest


@pytest.mark.parametrize(
    ("router", "name"),
    [
        (enterprise.router, "check_email_exists"),
        (enterprise.router, "probe_llm_model"),
        (enterprise.router, "get_tenant_quotas"),
        (enterprise.router, "get_system_setting"),
        (tenants.router, "resolve_tenant_by_domain"),
        (sso.router, "create_sso_session"),
        (sso.router, "get_sso_session_status"),
        (onboarding.router, "create_personal_assistant"),
        (agent_credentials.router, "create_credential"),
    ],
)
@pytest.mark.parametrize("payload", [{}, {"value": None}, {"exists": "false", "extra": [1, None]}])
async def test_unmodeled_wrappers_preserve_missing_null_extra_and_uncoerced_values(router, name, payload):
    route = next(route for route in router.routes if isinstance(route, APIRoute) and route.name == name)
    result = await serialize_response(field=route.response_field, response_content=payload)
    assert result == payload


async def test_credential_http_preserves_datetime_encoding_and_hides_cookies(monkeypatch):
    user = UserRecord(id=uuid4(), role="org_admin")
    agent_id = uuid4()
    cred = AgentCredentialRecord(
        id=uuid4(),
        agent_id=agent_id,
        platform="example",
        cookies_json="encrypted",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    monkeypatch.setattr(agent_credentials, "check_agent_access", AsyncMock(return_value=(None, "manage")))
    monkeypatch.setattr(agent_credentials.agent_credential_dao, "list_for_agent", AsyncMock(return_value=[cred]))
    app = FastAPI()
    app.include_router(agent_credentials.router)
    app.dependency_overrides[agent_credentials.get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/agents/{agent_id}/credentials/")
    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["id"] == str(cred.id)
    assert payload["created_at"] == "2026-01-02T00:00:00+00:00"
    assert payload["updated_at"] is None
    assert payload["has_cookies"] is True
    assert "cookies_json" not in payload
    app.dependency_overrides.clear()


@pytest.mark.parametrize("existing", [False, True])
async def test_assistant_http_returns_key_only_on_creation(monkeypatch, existing):
    tenant_id = uuid4()
    user = UserRecord(id=uuid4(), tenant_id=tenant_id)
    agent = AgentRecord(id=uuid4(), name="Assistant", creator_id=user.id)
    row = UserTenantOnboardingRecord(
        id=uuid4(), user_id=user.id, tenant_id=tenant_id, personal_assistant_agent_id=agent.id if existing else None
    )
    monkeypatch.setattr(onboarding, "_ensure_row", AsyncMock(return_value=row))
    monkeypatch.setattr(onboarding.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(onboarding, "_create_personal_assistant", AsyncMock(return_value=(agent, "one-time-key")))
    monkeypatch.setattr(onboarding.user_tenant_onboarding_dao, "update", AsyncMock(return_value=row))
    app = FastAPI()
    app.include_router(onboarding.router)
    app.dependency_overrides[onboarding.get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/onboarding/personal-assistant", json={"name": "Assistant"})
    assert response.status_code == 201
    assert response.json()["agent"] == (
        {"id": str(agent.id), "name": "Assistant"}
        if existing
        else {"id": str(agent.id), "name": "Assistant", "api_key": "one-time-key"}
    )
    assert response.json()["onboarding"]["completed_at"] is None
    app.dependency_overrides.clear()


@pytest.mark.parametrize("stored", [None, {"enabled": "false", "text": ["open", None]}])
async def test_public_notification_preserves_open_setting_values(monkeypatch, stored):
    setting = SystemSettingRecord(key="notification_bar", value=stored) if stored else None
    monkeypatch.setattr(enterprise.system_setting_dao, "get_by_key", AsyncMock(return_value=setting))
    app = FastAPI()
    app.include_router(enterprise.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/enterprise/system-settings/notification_bar/public")
    assert response.status_code == 200
    assert response.json() == (
        {"enabled": False, "text": "", "updated_at": None} if stored is None else {**stored, "updated_at": None}
    )


@pytest.mark.parametrize(
    "adapter",
    [
        TypeAdapter(enterprise_contracts.EmailExistsResponse),
        TypeAdapter(enterprise_contracts.LLMProbeResponse),
        TypeAdapter(enterprise_contracts.EnterpriseStatsResponse),
        TypeAdapter(enterprise_contracts.EmptyTenantQuotas | enterprise_contracts.TenantQuotasResponse),
        TypeAdapter(enterprise_contracts.TenantQuotasUpdatedResponse),
        TypeAdapter(enterprise_contracts.EmailSuccessResponse),
        TypeAdapter(enterprise_contracts.EmailTemplatesResponse),
        TypeAdapter(enterprise_contracts.NotificationBarResponse),
        TypeAdapter(enterprise_contracts.SystemSettingResponse),
        TypeAdapter(enterprise_contracts.IdentityProviderPayload),
        TypeAdapter(enterprise_contracts.InvitationCodesCreatedResponse),
        TypeAdapter(enterprise_contracts.UsersInvitedResponse),
        TypeAdapter(enterprise_contracts.InvitationCodesResponse),
        TypeAdapter(enterprise_contracts.InvitationDeactivatedResponse),
        TypeAdapter(tenant_contracts.TenantRegistrationResponse),
        TypeAdapter(tenant_contracts.TenantResolvedResponse),
        TypeAdapter(tenant_contracts.TenantTokenUsageResponse),
        TypeAdapter(tenant_contracts.TenantAssignmentResponse),
        TypeAdapter(tenant_contracts.TenantDeletedResponse),
        TypeAdapter(onboarding_contracts.OnboardingStatusPayload),
        TypeAdapter(onboarding_contracts.PersonalAssistantResponse),
        TypeAdapter(sso_contracts.SSOSessionResponse),
        TypeAdapter(sso_contracts.SSOStatusResponse),
        TypeAdapter(sso_contracts.SSOScannedResponse),
        TypeAdapter(list[sso_contracts.SSOProviderResponse]),
        TypeAdapter(agent_credentials.AgentCredentialPayload),
        TypeAdapter(list[ProviderManifest]),
        TypeAdapter(tenants.TenantDeletionPreview),
    ],
)
def test_response_contracts_support_pydantic_schema_generation(adapter):
    schema = adapter.json_schema()
    assert schema.get("type") in {"object", "array"} or "anyOf" in schema


@pytest.mark.parametrize("payload", [{}, {"exists": None}])
def test_contract_requires_non_nullable_fields(payload):
    adapter = TypeAdapter(enterprise_contracts.EmailExistsResponse)
    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


def test_contract_coercion_is_not_applied_to_legacy_routes():
    payload = {"exists": "false", "extra": [1, None]}
    result = TypeAdapter(enterprise_contracts.EmailExistsResponse).validate_python(payload)
    assert result == {"exists": False}


@pytest.mark.parametrize(
    "stored", [None, SystemSettingRecord(key="custom", value={"nested": [1, None, {"free": True}]})]
)
async def test_setting_http_preserves_omission_and_dynamic_json(monkeypatch, stored):
    user = UserRecord(id=uuid4())
    monkeypatch.setattr(enterprise.system_setting_dao, "get_by_key", AsyncMock(return_value=stored))
    app = FastAPI()
    app.include_router(enterprise.router)
    app.dependency_overrides[enterprise.get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/enterprise/system-settings/custom")
    expected = (
        {"key": "custom", "value": {}}
        if stored is None
        else {"key": "custom", "value": stored.value, "updated_at": None}
    )
    assert response.status_code == 200
    assert response.json() == expected
    assert TypeAdapter(enterprise_contracts.SystemSettingResponse).validate_python(response.json()) == expected
    app.dependency_overrides.clear()


@pytest.mark.parametrize("redeemed", [False, True])
async def test_sso_http_preserves_one_time_credentials_and_user_encoding(monkeypatch, redeemed):
    _, user, session_id = install_session(monkeypatch)
    user.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    if redeemed:
        await sso.get_sso_session_status(session_id)
    app = FastAPI()
    app.include_router(sso.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/sso/session/{session_id}/status")
    assert response.status_code == 200
    payload = response.json()
    if redeemed:
        assert payload == {"status": "completed", "provider_type": "feishu", "error_msg": None}
    else:
        assert set(payload) == {"status", "provider_type", "error_msg", "access_token", "user"}
        assert payload["user"]["id"] == str(user.id)
        assert payload["user"]["created_at"] == "2026-01-02T00:00:00+00:00"
        assert payload["user"]["avatar_url"] is None


def test_provider_payload_preserves_existing_model_dump_contract():
    provider = IdentityProviderRecord(
        uuid4(), "oauth2", "Example", created_at=datetime(2026, 1, 2, tzinfo=UTC), config={"dynamic": [None, 7]}
    )
    payload = enterprise._identity_provider_response(provider)
    assert isinstance(payload, dict)
    assert payload == enterprise.IdentityProviderOut.model_validate(provider).model_dump()
    assert TypeAdapter(enterprise_contracts.IdentityProviderPayload).validate_python(payload) == payload
