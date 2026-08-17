"""LLM pool authorization, serialization, and tenant-model checks."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status

from app.dao.agent_dao import agent_dao
from app.dao.llm_dao import llm_model_dao
from app.dao.tenant_dao import tenant_dao
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.schemas.schemas import LLMModelOut
from app.services.llm import get_model_api_key


def is_platform_admin(user: Any) -> bool:
    """True for tenant-role or identity-level platform admins."""
    return user.role == "platform_admin" or bool(
        getattr(getattr(user, "identity", None), "is_platform_admin", False)
    )


def is_llm_pool_admin(user: Any) -> bool:
    """Org admins and platform admins may configure the company LLM pool."""
    return is_platform_admin(user) or user.role == "org_admin"


def assert_llm_pool_admin(user: Any) -> None:
    """Reject members and agent admins from provider configuration."""
    if not is_llm_pool_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an organization admin can configure LLM providers",
        )


def resolve_llm_pool_tenant_id(user: Any, requested: str | None) -> uuid.UUID | None:
    """Resolve the tenant whose model pool the caller may see or mutate.

    Platform admins may pass any ``tenant_id`` or omit it (all / global).
    Everyone else is locked to their own company. Members without a tenant
    get ``None`` so callers can return an empty list instead of every row.
    """
    if requested:
        try:
            tid = uuid.UUID(requested)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid tenant_id") from None
        if not is_platform_admin(user) and str(getattr(user, "tenant_id", None)) != requested:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot access another company's models",
            )
        return tid

    own = getattr(user, "tenant_id", None)
    if own is None:
        return None
    return own if isinstance(own, uuid.UUID) else uuid.UUID(str(own))


def require_llm_pool_tenant_id(user: Any, requested: str | None) -> uuid.UUID:
    """Resolve the company for a pool write. Platform admins must pass one."""
    assert_llm_pool_admin(user)
    if is_platform_admin(user) and not requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select a company to add a model",
        )
    tid = resolve_llm_pool_tenant_id(user, requested)
    if tid is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No company is assigned")
    return tid


def assert_can_manage_model(user: Any, model: LLMModelRecord) -> None:
    """Org admins may only mutate models that belong to their company."""
    assert_llm_pool_admin(user)
    if is_platform_admin(user):
        return
    if model.tenant_id is None or model.tenant_id != getattr(user, "tenant_id", None):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot manage another company's models",
        )


def serialize_llm_model(
    model: LLMModelRecord,
    *,
    is_admin: bool,
    default_model_id: uuid.UUID | None,
    fallback_model_id: uuid.UUID | None = None,
    secondary_model_id: uuid.UUID | None = None,
) -> LLMModelOut:
    """Build an API payload. Members never receive keys or endpoints."""
    out = LLMModelOut.model_validate(model)
    out.is_default = default_model_id is not None and model.id == default_model_id
    out.is_fallback = fallback_model_id is not None and model.id == fallback_model_id
    out.is_secondary = secondary_model_id is not None and model.id == secondary_model_id
    if is_admin:
        key = get_model_api_key(model)
        out.api_key_masked = f"****{key[-4:]}" if len(key) > 4 else "****"
        return out
    out.api_key_masked = ""
    out.base_url = None
    out.request_timeout = None
    out.max_tokens_per_day = None
    return out


def model_usable_in_tenant(model: LLMModelRecord, tenant_id: uuid.UUID | None) -> bool:
    """True only when ``model`` is an enabled row owned by ``tenant_id``.

    Global (``tenant_id is None``) and other-company rows are never usable by
    members or agents. End users inherit only their org's pool.
    """
    if not model.enabled or tenant_id is None or model.tenant_id is None:
        return False
    return model.tenant_id == tenant_id


def owned_model_or_none(model: LLMModelRecord | None, tenant_id: uuid.UUID | None) -> LLMModelRecord | None:
    """Return ``model`` when it belongs to ``tenant_id``, otherwise ``None``."""
    if model is None or not model_usable_in_tenant(model, tenant_id):
        return None
    return model


def is_grok_family(model: LLMModelRecord | None) -> bool:
    provider = (getattr(model, "provider", None) or "").strip().lower()
    return provider in {"grok", "xai", "x-ai", "x_ai"}


def is_grok_api_key_row(model: LLMModelRecord | None) -> bool:
    """True for a company Grok/xAI console-key row (not SuperGrok OAuth)."""
    if model is None or not is_grok_family(model):
        return False
    return (getattr(model, "auth_kind", None) or "api_key") != "grok_subscription"


def assert_distinct_model_slots(
    primary_id: uuid.UUID | None,
    secondary_id: uuid.UUID | None,
    fallback_id: uuid.UUID | None,
) -> None:
    """Reject overlapping primary / secondary / fallback assignments."""
    filled = [mid for mid in (primary_id, secondary_id, fallback_id) if mid is not None]
    if len(filled) != len(set(filled)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Primary, secondary, and fallback models must be different",
        )


async def activate_pool_model_for_tenant(model: LLMModelRecord) -> None:
    """If the company has no primary, make this row the default and fill bare agents."""
    if model.tenant_id is None or not model.enabled:
        return
    tenant = await tenant_dao.get(model.tenant_id)
    if tenant is None:
        return
    if tenant.default_model_id is None:
        _ = await tenant_dao.update(db_obj=tenant, obj_in={"default_model_id": model.id})
        _ = await agent_dao.assign_primary_where_null(tenant_id=model.tenant_id, model_id=model.id)
        return
    current_default = await llm_model_dao.get(tenant.default_model_id)
    if getattr(model, "auth_kind", "") == "grok_subscription" and (
        current_default is None or is_grok_api_key_row(current_default)
    ):
        _ = await tenant_dao.update(db_obj=tenant, obj_in={"default_model_id": model.id})
        for row in await llm_model_dao.list_for_tenant(model.tenant_id):
            if row.id != model.id and is_grok_api_key_row(row):
                _ = await agent_dao.migrate_primary_model(
                    tenant_id=model.tenant_id, old_model_id=row.id, new_model_id=model.id
                )
        _ = await agent_dao.assign_primary_where_null(tenant_id=model.tenant_id, model_id=model.id)
        return
    if getattr(tenant, "default_fallback_model_id", None) is None and tenant.default_model_id != model.id:
        _ = await tenant_dao.update(db_obj=tenant, obj_in={"default_fallback_model_id": model.id})
    _ = await agent_dao.assign_primary_where_null(tenant_id=model.tenant_id, model_id=tenant.default_model_id)


async def _first_usable_tenant_model(tenant_id: uuid.UUID) -> LLMModelRecord | None:
    rows = await llm_model_dao.list_for_tenant(tenant_id)
    owned = [row for row in rows if model_usable_in_tenant(row, tenant_id)]
    if not owned:
        return None
    for row in owned:
        if getattr(row, "auth_kind", "") == "grok_subscription":
            return row
    return owned[0]


def _slot_replacement(
    assigned_id: uuid.UUID | None,
    owned: LLMModelRecord | None,
    company: LLMModelRecord | None,
) -> uuid.UUID | object | None:
    """Return a new slot id, or ``_UNCHANGED`` when the current assignment is valid."""
    if owned is not None:
        return _UNCHANGED
    if company is not None:
        return company.id
    if assigned_id is not None:
        return None
    return _UNCHANGED


_UNCHANGED = object()


async def ensure_agent_company_models(agent: AgentRecord) -> AgentRecord:
    """Keep agent slots on this company's pool; replace foreign or empty slots."""
    raw_tenant = getattr(agent, "tenant_id", None)
    if not isinstance(raw_tenant, uuid.UUID):
        return agent
    tenant_id = raw_tenant
    tenant = await tenant_dao.get(tenant_id)
    if tenant is None:
        return agent

    wanted = [
        getattr(agent, "primary_model_id", None),
        getattr(agent, "secondary_model_id", None),
        getattr(agent, "fallback_model_id", None),
        tenant.default_model_id,
        getattr(tenant, "default_secondary_model_id", None),
        getattr(tenant, "default_fallback_model_id", None),
    ]
    loaded = {row.id: row for row in await llm_model_dao.get_many([mid for mid in wanted if mid])}

    def owned(mid: uuid.UUID | None) -> LLMModelRecord | None:
        return owned_model_or_none(loaded.get(mid) if mid else None, tenant_id)

    tenant_primary = owned(tenant.default_model_id)
    subscription = None
    if tenant_primary is None or is_grok_api_key_row(tenant_primary):
        subscription = await llm_model_dao.get_subscription_for_tenant(tenant_id)
        if subscription is not None and model_usable_in_tenant(subscription, tenant_id):
            loaded[subscription.id] = subscription
            if tenant_primary is None or is_grok_api_key_row(tenant_primary):
                tenant_primary = subscription
                if tenant.default_model_id != subscription.id:
                    _ = await tenant_dao.update(db_obj=tenant, obj_in={"default_model_id": subscription.id})
                    _ = await agent_dao.assign_primary_where_null(tenant_id=tenant_id, model_id=subscription.id)
    if tenant_primary is None:
        picked = await _first_usable_tenant_model(tenant_id)
        if picked is not None:
            tenant_primary = picked
            loaded[picked.id] = picked
            if tenant.default_model_id != picked.id:
                _ = await tenant_dao.update(db_obj=tenant, obj_in={"default_model_id": picked.id})
                _ = await agent_dao.assign_primary_where_null(tenant_id=tenant_id, model_id=picked.id)

    tenant_secondary = owned(getattr(tenant, "default_secondary_model_id", None))
    tenant_fallback = owned(getattr(tenant, "default_fallback_model_id", None))
    updates: dict[str, uuid.UUID | None] = {}
    agent_primary = owned(agent.primary_model_id)
    if (
        is_grok_api_key_row(agent_primary)
        and tenant_primary is not None
        and getattr(tenant_primary, "auth_kind", "") == "grok_subscription"
    ):
        updates["primary_model_id"] = tenant_primary.id
    else:
        primary_next = _slot_replacement(
            getattr(agent, "primary_model_id", None), agent_primary, tenant_primary
        )
        if primary_next is not _UNCHANGED:
            updates["primary_model_id"] = primary_next if isinstance(primary_next, uuid.UUID) else None
    secondary_next = _slot_replacement(
        getattr(agent, "secondary_model_id", None),
        owned(getattr(agent, "secondary_model_id", None)),
        tenant_secondary,
    )
    if secondary_next is not _UNCHANGED:
        updates["secondary_model_id"] = secondary_next if isinstance(secondary_next, uuid.UUID) else None
    fallback_next = _slot_replacement(
        getattr(agent, "fallback_model_id", None),
        owned(getattr(agent, "fallback_model_id", None)),
        tenant_fallback,
    )
    if fallback_next is not _UNCHANGED:
        updates["fallback_model_id"] = fallback_next if isinstance(fallback_next, uuid.UUID) else None
    if not updates:
        return agent
    updated = await agent_dao.update(db_obj=agent, obj_in=updates)
    if updated is None:
        for key, value in updates.items():
            setattr(agent, key, value)
        return agent
    return updated


async def assert_models_in_tenant_pool(
    tenant_id: uuid.UUID | None,
    *model_ids: uuid.UUID | None,
) -> None:
    """Reject agent model assignments that are missing, disabled, or off-tenant."""
    wanted = [mid for mid in model_ids if mid is not None]
    if not wanted:
        return
    loaded = {row.id: row for row in await llm_model_dao.get_many(wanted)}
    for mid in wanted:
        model = loaded.get(mid)
        if model is None or not model_usable_in_tenant(model, tenant_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Model is not available to this company",
            )
