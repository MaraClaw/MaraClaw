"""Encrypted company ChatGPT credentials, activation, and refresh."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Final

from fastapi import HTTPException, status

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.dao.llm_dao import llm_model_dao
from app.db.session import optional_connection_ctx
from app.records.llm import LLMModelRecord
from app.records.user import UserRecord
from app.services.chatgpt_oauth import CHATGPT_CODEX_BASE_URL, ChatGPTOAuthTokens, refresh_access_token
from app.services.chatgpt_subscription_types import ChatGPTSubscriptionRefreshOut
from app.services.enterprise_llm import activate_pool_model_for_tenant, assert_can_manage_model
from app.services.llm import get_model_api_key
from app.services.subscription_boundaries import unchanged_subscription_model

AUTH_KIND_API_KEY: Final = "api_key"
AUTH_KIND_CHATGPT_SUBSCRIPTION: Final = "chatgpt_subscription"
CHATGPT_SUBSCRIPTION_PROVIDER: Final = "openai"
CHATGPT_SUBSCRIPTION_MODEL: Final = "gpt-6-luna"
CHATGPT_SUBSCRIPTION_LABEL: Final = "ChatGPT"
REFRESH_SKEW = timedelta(minutes=5)


def _encrypt(value: str) -> str:
    return encrypt_data(value, get_settings().SECRET_KEY)


def _decrypt(value: str | None) -> str:
    if value in (None, ""):
        return ""
    try:
        return decrypt_data(value, get_settings().SECRET_KEY)
    except ValueError:
        return value


async def persist_subscription_tokens(
    tenant_id: uuid.UUID, tokens: ChatGPTOAuthTokens, *, existing: LLMModelRecord | None = None
) -> LLMModelRecord:
    async with optional_connection_ctx():
        return await _persist_subscription_tokens(tenant_id, tokens, existing=existing)


async def _persist_subscription_tokens(
    tenant_id: uuid.UUID, tokens: ChatGPTOAuthTokens, *, existing: LLMModelRecord | None = None
) -> LLMModelRecord:
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens.expires_in)
    updates: dict[str, object] = {
        "api_key_encrypted": _encrypt(tokens.access_token),
        "token_expires_at": expires_at,
        "auth_kind": AUTH_KIND_CHATGPT_SUBSCRIPTION,
        "enabled": True,
    }
    if tokens.refresh_token:
        updates["refresh_token_encrypted"] = _encrypt(tokens.refresh_token)
    if tokens.account_id:
        updates["oauth_account_id"] = tokens.account_id
    row = existing or await llm_model_dao.get_subscription_for_tenant(
        tenant_id, auth_kind=AUTH_KIND_CHATGPT_SUBSCRIPTION
    )
    if row is not None:
        saved = await llm_model_dao.update(db_obj=row, obj_in=updates)
    else:
        saved = await llm_model_dao.create(
            obj_in={
                **updates,
                "provider": CHATGPT_SUBSCRIPTION_PROVIDER,
                "model": CHATGPT_SUBSCRIPTION_MODEL,
                "base_url": CHATGPT_CODEX_BASE_URL,
                "label": CHATGPT_SUBSCRIPTION_LABEL,
                "tenant_id": tenant_id,
                "supports_vision": True,
            }
        )
    await activate_pool_model_for_tenant(saved)
    return saved


async def refresh_subscription_model(model: LLMModelRecord, *, actor: UserRecord | None = None) -> LLMModelRecord:
    """Refresh outside the DB checkout; recheck authority and the row before writing."""
    kind = getattr(model, "auth_kind", AUTH_KIND_API_KEY) or AUTH_KIND_API_KEY
    if kind != AUTH_KIND_CHATGPT_SUBSCRIPTION:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a ChatGPT subscription model")
    refresh = _decrypt(getattr(model, "refresh_token_encrypted", None))
    if not refresh:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ChatGPT subscription has no refresh token; connect again",
        )
    poll = await refresh_access_token(refresh)
    if poll.status != "authorized" or poll.tokens is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=poll.error or "ChatGPT subscription refresh failed",
        )
    tokens = poll.tokens
    if not tokens.refresh_token:
        tokens = replace(tokens, refresh_token=refresh)
    if model.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Model is not tenant-scoped")
    async with optional_connection_ctx():
        current = await unchanged_subscription_model(model, actor)
        return await persist_subscription_tokens(model.tenant_id, tokens, existing=current)


def _is_fresh(model: LLMModelRecord) -> bool:
    expires = model.token_expires_at
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > datetime.now(UTC) + REFRESH_SKEW


async def ensure_fresh_access_token(model: LLMModelRecord, *, actor: UserRecord | None = None) -> LLMModelRecord:
    kind = getattr(model, "auth_kind", AUTH_KIND_API_KEY) or AUTH_KIND_API_KEY
    if kind != AUTH_KIND_CHATGPT_SUBSCRIPTION:
        return model
    if _is_fresh(model) and get_model_api_key(model):
        return model
    try:
        return await refresh_subscription_model(model, actor=actor)
    except HTTPException:
        if actor is not None:
            raise
        return model


async def refresh_chatgpt_subscription_for_admin(
    user: UserRecord, model_id: uuid.UUID
) -> ChatGPTSubscriptionRefreshOut:
    async with optional_connection_ctx():
        model = await llm_model_dao.get(model_id)
        if model is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found")
        assert_can_manage_model(user, model)
    updated = await refresh_subscription_model(model, actor=user)
    return ChatGPTSubscriptionRefreshOut(ok=True, model_id=updated.id, expires_at=updated.token_expires_at)
