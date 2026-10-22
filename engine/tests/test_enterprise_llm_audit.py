"""LLM audit semantics through the mutation owners and real audit DAO."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import enterprise
from app.records.llm import LLMModelRecord
from app.records.tenant import TenantRecord
from app.schemas.schemas import LLMModelCreate, LLMModelUpdate

if TYPE_CHECKING:
    from tests.enterprise_audit_fakes import AuditDatabase

pytest_plugins = ("tests.enterprise_audit_fakes",)
_SLOTS = ("default_model_id", "default_secondary_model_id", "default_fallback_model_id")
_ASSIGNMENTS = {
    "set_default": enterprise.set_default_llm_model,
    "set_secondary": enterprise.set_secondary_llm_model,
    "set_fallback": enterprise.set_fallback_llm_model,
}


def _assert_redacted(event: dict[str, object], model: LLMModelRecord) -> None:
    payload = json.dumps(event, default=str)
    for secret in (
        "secret-key-9999",
        model.api_key_encrypted,
        "****9999",
        "new-secret",
        "url-user",
        "url-password",
        "url-token",
        "url-fragment",
        "refresh-secret",
        "oauth-account",
        "new-user",
        "new-password",
        "new-token",
    ):
        assert secret not in payload


@pytest.mark.parametrize("slot", [None, "default_model_id", "default_fallback_model_id"])
async def test_create_audits_implicit_assignment_for_affected_tenant(audit_db: AuditDatabase, slot: str | None) -> None:
    # Given
    tenant = audit_db.rows["tenants"][audit_db.tenant.id]
    if slot != "default_model_id":
        tenant["default_model_id"] = uuid4()
    if slot is None:
        tenant["default_fallback_model_id"] = uuid4()
    actor = replace(audit_db.actor, role="platform_admin", tenant_id=uuid4())
    # When
    result = await enterprise.add_llm_model(
        LLMModelCreate(
            provider="anthropic",
            model="new-model",
            api_key="new-secret",
            base_url=audit_db.model.base_url,
            label="",
            temperature=0.2,
            reasoning_effort=None,
        ),
        str(audit_db.tenant.id),
        actor,
    )
    # Then
    [event] = audit_db.events
    expected: dict[str, object] = {"created": {"before": False, "after": True}}
    if slot is not None:
        expected[slot] = {"before": None, "after": str(result.id)}
    assert event["action"] == "llm_model_create"
    assert event["tenant_id"] == audit_db.tenant.id != actor.tenant_id
    assert event["target_id"] == result.id
    assert event["changes"] == expected
    details = event["details"]
    assert isinstance(details, dict)
    assert details["credential_changed"] is True
    _assert_redacted(event, LLMModelRecord.from_row(audit_db.rows["llm_models"][result.id]))


async def test_update_snapshots_semantic_values_before_mutable_dao_update(
    audit_db: AuditDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    original_update = enterprise.llm_model_dao.update

    async def mutate(*, db_obj: LLMModelRecord, obj_in: Mapping[str, object]) -> LLMModelRecord:
        await original_update(db_obj=db_obj, obj_in=obj_in)
        for field, value in obj_in.items():
            setattr(db_obj, field, value)
        return db_obj

    monkeypatch.setattr(enterprise.llm_model_dao, "update", mutate)
    before_ciphertext = audit_db.model.api_key_encrypted
    actor = replace(audit_db.actor, role="platform_admin", tenant_id=uuid4())
    # When
    await enterprise.update_llm_model(
        audit_db.model.id,
        LLMModelUpdate(
            label="Renamed",
            temperature=0.7,
            api_key="new-secret",
            base_url="https://new-user:new-password@provider.invalid/v2?token=new-token#url-fragment",
            reasoning_effort=None,
        ),
        actor,
    )
    # Then
    [event] = audit_db.events
    assert event["tenant_id"] == audit_db.tenant.id != actor.tenant_id
    assert event["changes"] == {
        "label": {"before": "Claude", "after": "Renamed"},
        "temperature": {"before": 0.2, "after": 0.7},
    }
    assert event["details"] == {"credential_changed": True, "base_url_changed": True}
    assert before_ciphertext not in json.dumps(event, default=str)
    _assert_redacted(event, LLMModelRecord.from_row(audit_db.rows["llm_models"][audit_db.model.id]))


@pytest.mark.parametrize("kind", ["api_key", "grok_subscription", "chatgpt_subscription"])
async def test_masked_or_subscription_credentials_are_not_reported_replaced(audit_db: AuditDatabase, kind: str) -> None:
    # Given
    model = audit_db.rows["llm_models"][audit_db.model.id]
    model.update(auth_kind=kind, refresh_token_encrypted="refresh-secret", oauth_account_id="oauth-account")
    # When
    await enterprise.update_llm_model(
        audit_db.model.id,
        LLMModelUpdate(
            label="Renamed",
            api_key="****9999" if kind == "api_key" else "new-secret",
            temperature=None,
            reasoning_effort=None,
        ),
        audit_db.actor,
    )
    # Then
    [event] = audit_db.events
    assert event["details"] == {"credential_changed": False, "base_url_changed": False}
    _assert_redacted(event, audit_db.model)


@pytest.mark.parametrize("operation", ["set_default", "set_secondary", "set_fallback", "disable", "delete"])
async def test_audit_captures_all_effective_slot_changes(
    audit_db: AuditDatabase, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    # Given
    tenant = audit_db.rows["tenants"][audit_db.tenant.id]
    before: dict[str, UUID] = {slot: uuid4() for slot in _SLOTS}
    updates: dict[str, UUID | None] = {}
    if operation in {"disable", "delete"}:
        before = dict.fromkeys(_SLOTS, audit_db.model.id)
        updates.update(default_model_id=None, default_secondary_model_id=None, default_fallback_model_id=None)
    elif operation == "set_default":
        before["default_secondary_model_id"] = before["default_fallback_model_id"] = audit_db.model.id
        updates.update(default_model_id=None, default_secondary_model_id=None, default_fallback_model_id=None)
        updates["default_model_id"] = audit_db.model.id
    else:
        updates["default_secondary_model_id" if operation == "set_secondary" else "default_fallback_model_id"] = (
            audit_db.model.id
        )
    tenant.update(before)
    original_update = enterprise.tenant_dao.update

    async def mutate(*, db_obj: TenantRecord, obj_in: Mapping[str, object]) -> TenantRecord:
        await original_update(db_obj=db_obj, obj_in=obj_in)
        for field, value in obj_in.items():
            setattr(db_obj, field, value)
        return db_obj

    monkeypatch.setattr(enterprise.tenant_dao, "update", mutate)
    actor = replace(audit_db.actor, role="platform_admin", tenant_id=uuid4())
    # When
    if operation == "disable":
        await enterprise.update_llm_model(
            audit_db.model.id, LLMModelUpdate(enabled=False, temperature=None, reasoning_effort=None), actor
        )
    elif operation == "delete":
        await enterprise.remove_llm_model(audit_db.model.id, False, actor)
    else:
        await _ASSIGNMENTS[operation](audit_db.model.id, actor)
    # Then
    [event] = audit_db.events
    expected: dict[str, object] = {
        slot: {"before": str(before[slot]), "after": str(value) if value else None} for slot, value in updates.items()
    }
    if operation == "disable":
        expected["enabled"] = {"before": True, "after": False}
    elif operation == "delete":
        expected["deleted"] = {"before": False, "after": True}
    assert event["changes"] == expected
    assert event["action"] == f"llm_model_{'update' if operation == 'disable' else operation}"
    assert event["tenant_id"] == audit_db.tenant.id != actor.tenant_id
    _assert_redacted(event, audit_db.model)


@pytest.mark.parametrize("operation", ["create", "update", "delete", "set_default", "set_secondary", "set_fallback"])
async def test_foreign_tenant_rejection_leaves_no_writes_or_audit(audit_db: AuditDatabase, operation: str) -> None:
    # Given
    actor = replace(audit_db.actor, tenant_id=uuid4())
    if operation == "create":
        mutation = enterprise.add_llm_model(
            LLMModelCreate(
                provider="anthropic",
                model="foreign",
                api_key="new-secret",
                label="",
                temperature=0.2,
                reasoning_effort=None,
            ),
            str(audit_db.tenant.id),
            actor,
        )
    elif operation == "update":
        mutation = enterprise.update_llm_model(
            audit_db.model.id, LLMModelUpdate(label="Forbidden", temperature=None, reasoning_effort=None), actor
        )
    elif operation == "delete":
        mutation = enterprise.remove_llm_model(audit_db.model.id, True, actor)
    else:
        mutation = _ASSIGNMENTS[operation](audit_db.model.id, actor)
    # When
    with pytest.raises(HTTPException) as exc:
        await mutation
    # Then
    assert exc.value.status_code == 403
    assert audit_db.events == []
    assert all(statement.startswith("SELECT ") for statement in audit_db.statements)
    assert audit_db.commits == 0
    assert audit_db.rollbacks == 1


@pytest.mark.parametrize("foreign", [False, True])
async def test_http_update_preserves_response_and_authorization(audit_db: AuditDatabase, foreign: bool) -> None:
    # Given
    app = FastAPI()
    app.include_router(enterprise.router, prefix="/api")
    actor = replace(audit_db.actor, tenant_id=uuid4()) if foreign else audit_db.actor
    app.dependency_overrides[enterprise.get_current_admin] = lambda: actor
    # When
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(f"/api/enterprise/llm-models/{audit_db.model.id}", json={"label": "Renamed"})
    finally:
        app.dependency_overrides.clear()
    # Then
    assert response.status_code == (403 if foreign else 200)
    assert len(audit_db.events) == (0 if foreign else 1)
    if not foreign:
        assert response.json()["id"] == str(audit_db.model.id)
        assert response.json()["label"] == "Renamed"
        _assert_redacted(audit_db.events[0], audit_db.model)
