"""LLM pool authorization, serialization, and tenant-model checks."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status

from app.dao.llm_dao import llm_model_dao
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
    """True when a stored model may be assigned to an agent in ``tenant_id``."""
    if not model.enabled:
        return False
    if model.tenant_id is None:
        return True
    return tenant_id is not None and model.tenant_id == tenant_id


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
