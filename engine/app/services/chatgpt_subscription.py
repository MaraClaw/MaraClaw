"""Company-level ChatGPT Plus / Pro / Team handoff into the LLM pool.

web-a only starts and polls. Tokens stay encrypted on the tenant pool row.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final, Literal, cast

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.dao.llm_dao import llm_model_dao
from app.records.llm import LLMModelRecord
from app.services.chatgpt_oauth import (
    ChatGPTOAuthTokens,
    DeviceCodeChallenge,
    fetch_userinfo,
    poll_device_authorization,
    refresh_access_token,
    request_device_code,
)
from app.services.enterprise_llm import (
    activate_pool_model_for_tenant,
    assert_can_manage_model,
    require_llm_pool_tenant_id,
)
from app.services.llm import get_model_api_key

AUTH_KIND_API_KEY: Final = "api_key"
AUTH_KIND_CHATGPT_SUBSCRIPTION: Final = "chatgpt_subscription"
CHATGPT_SUBSCRIPTION_PROVIDER: Final = "openai"
CHATGPT_SUBSCRIPTION_MODEL: Final = "gpt-5.4"
CHATGPT_SUBSCRIPTION_LABEL: Final = "ChatGPT"
CHATGPT_SUBSCRIPTION_BASE_URL: Final = "https://chatgpt.com/backend-api/codex"
REFRESH_SKEW = timedelta(minutes=5)

ChatGPTStatus = Literal["pending", "authorized", "expired", "denied", "error"]


class ChatGPTSubscriptionStartOut(BaseModel):
    """Human-facing start payload. Never includes tokens or device_auth_id."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    session_id: str
    verification_url: str
    user_code: str
    expires_in: int
    interval: int


class ChatGPTSubscriptionStatusOut(BaseModel):
    """Human-facing poll payload. Tokens stay on the server."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    status: ChatGPTStatus
    session_id: str
    verification_url: str | None = None
    user_code: str | None = None
    model_id: uuid.UUID | None = None
    detail: str | None = None
    interval: int | None = None


class ChatGPTSubscriptionRefreshOut(BaseModel):
    """Refresh acknowledgement. No token fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    ok: bool
    model_id: uuid.UUID
    expires_at: datetime | None = None


@dataclass(slots=True)
class ChatGPTOAuthSession:
    """In-process pending device-code session. device_auth_id never leaves this module."""

    session_id: str
    tenant_id: uuid.UUID
    device_auth_id: str
    user_code: str
    verification_url: str
    interval: int
    expires_at: datetime
    status: ChatGPTStatus = "pending"
    model_id: uuid.UUID | None = None
    detail: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


_SESSIONS: dict[str, ChatGPTOAuthSession] = {}

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "device_code",
        "device_auth_id",
        "authorization_code",
        "code_verifier",
        "code_challenge",
        "cookie",
        "cookies",
        "password",
        "api_key",
        "token",
    }
)


def reset_chatgpt_sessions() -> None:
    """Drop pending sessions. Tests call this between cases."""
    _SESSIONS.clear()


def _assert_public(payload: BaseModel) -> None:
    dumped = payload.model_dump()
    leaked = _SECRET_KEYS.intersection(dumped)
    if leaked:
        raise RuntimeError(f"refusing to expose secret fields: {sorted(leaked)}")


def public_start_payload(session: ChatGPTOAuthSession) -> ChatGPTSubscriptionStartOut:
    remaining = max(int((session.expires_at - datetime.now(UTC)).total_seconds()), 0)
    out = ChatGPTSubscriptionStartOut(
        session_id=session.session_id,
        verification_url=session.verification_url,
        user_code=session.user_code,
        expires_in=remaining,
        interval=session.interval,
    )
    _assert_public(out)
    return out


def public_status_payload(session: ChatGPTOAuthSession) -> ChatGPTSubscriptionStatusOut:
    out = ChatGPTSubscriptionStatusOut(
        status=session.status,
        session_id=session.session_id,
        verification_url=session.verification_url if session.status == "pending" else None,
        user_code=session.user_code if session.status == "pending" else None,
        model_id=session.model_id,
        detail=session.detail,
        interval=session.interval if session.status == "pending" else None,
    )
    _assert_public(out)
    return out


def _store_session(challenge: DeviceCodeChallenge, tenant_id: uuid.UUID) -> ChatGPTOAuthSession:
    session = ChatGPTOAuthSession(
        session_id=secrets.token_urlsafe(32),
        tenant_id=tenant_id,
        device_auth_id=challenge.device_auth_id,
        user_code=challenge.user_code,
        verification_url=challenge.verification_url,
        interval=challenge.interval,
        expires_at=datetime.now(UTC) + timedelta(seconds=challenge.expires_in),
    )
    _SESSIONS[session.session_id] = session
    return session


def _get_session(session_id: str) -> ChatGPTOAuthSession:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown ChatGPT subscription session"
        )
    return session


def _require_session_tenant(user: Any, session: ChatGPTOAuthSession) -> uuid.UUID:
    return require_llm_pool_tenant_id(user, str(session.tenant_id))


def _encrypt(value: str) -> str:
    return encrypt_data(value, get_settings().SECRET_KEY)


def _decrypt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return decrypt_data(value, get_settings().SECRET_KEY)
    except ValueError:
        return value


async def persist_subscription_tokens(
    tenant_id: uuid.UUID,
    tokens: ChatGPTOAuthTokens,
    *,
    existing: LLMModelRecord | None = None,
) -> LLMModelRecord:
    """Upsert the company ChatGPT subscription pool row. Access + refresh stay encrypted."""
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens.expires_in)
    updates: dict[str, object] = {
        "api_key_encrypted": _encrypt(tokens.access_token),
        "token_expires_at": expires_at,
        "auth_kind": AUTH_KIND_CHATGPT_SUBSCRIPTION,
        "enabled": True,
        "provider": CHATGPT_SUBSCRIPTION_PROVIDER,
        "model": CHATGPT_SUBSCRIPTION_MODEL,
        "base_url": CHATGPT_SUBSCRIPTION_BASE_URL,
    }
    if tokens.refresh_token:
        updates["refresh_token_encrypted"] = _encrypt(tokens.refresh_token)
    row = existing or await llm_model_dao.get_subscription_for_tenant(
        tenant_id, auth_kind=AUTH_KIND_CHATGPT_SUBSCRIPTION
    )
    if row is not None:
        saved = await llm_model_dao.update(db_obj=row, obj_in=updates)
    else:
        saved = await llm_model_dao.create(
            obj_in={
                **updates,
                "label": CHATGPT_SUBSCRIPTION_LABEL,
                "tenant_id": tenant_id,
                "supports_vision": True,
            }
        )
    await activate_pool_model_for_tenant(saved)
    return saved


async def start_chatgpt_subscription_handoff(user: Any, tenant_id: str | None) -> ChatGPTSubscriptionStartOut:
    """Admin-only device-code start. Invokes the shipped Codex start path."""
    tid = require_llm_pool_tenant_id(user, tenant_id)
    challenge = await request_device_code()
    session = _store_session(challenge, tid)
    return public_start_payload(session)


async def chatgpt_subscription_status(user: Any, session_id: str) -> ChatGPTSubscriptionStatusOut:
    """Poll OpenAI once and, on success, persist an encrypted company pool row."""
    session = _get_session(session_id)
    _require_session_tenant(user, session)
    if session.status != "pending":
        return public_status_payload(session)
    if datetime.now(UTC) >= session.expires_at:
        session.status = "expired"
        session.detail = "Device-code sign-in expired"
        session.device_auth_id = ""
        return public_status_payload(session)

    poll = await poll_device_authorization(
        session.device_auth_id, session.user_code, interval=session.interval
    )
    if poll.interval:
        session.interval = poll.interval
    if poll.status == "pending":
        return public_status_payload(session)
    if poll.status == "authorized" and poll.tokens is not None:
        row = await persist_subscription_tokens(session.tenant_id, poll.tokens)
        session.status = "authorized"
        session.model_id = row.id
        session.detail = "ChatGPT subscription connected"
        session.device_auth_id = ""
        return public_status_payload(session)
    next_status: ChatGPTStatus = cast(
        ChatGPTStatus, poll.status if poll.status in {"denied", "expired", "error"} else "error"
    )
    session.status = next_status
    session.detail = poll.error or "ChatGPT subscription sign-in failed"
    session.device_auth_id = ""
    return public_status_payload(session)


async def refresh_subscription_model(model: LLMModelRecord) -> LLMModelRecord:
    """Refresh a stored subscription without a new browser login. Shipped refresh path."""
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
        tokens = ChatGPTOAuthTokens(
            access_token=tokens.access_token,
            refresh_token=refresh,
            expires_in=tokens.expires_in,
            token_type=tokens.token_type,
            scope=tokens.scope,
            id_token=tokens.id_token,
            account_id=tokens.account_id,
        )
    if model.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Model is not tenant-scoped")
    return await persist_subscription_tokens(model.tenant_id, tokens, existing=model)


def _is_fresh(model: LLMModelRecord) -> bool:
    expires = getattr(model, "token_expires_at", None)
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > datetime.now(UTC) + REFRESH_SKEW


async def ensure_fresh_access_token(model: LLMModelRecord) -> LLMModelRecord:
    """Refresh an expired (or soon-expiring) subscription; otherwise return as-is."""
    kind = getattr(model, "auth_kind", AUTH_KIND_API_KEY) or AUTH_KIND_API_KEY
    if kind != AUTH_KIND_CHATGPT_SUBSCRIPTION:
        return model
    if _is_fresh(model) and get_model_api_key(model):
        return model
    try:
        return await refresh_subscription_model(model)
    except HTTPException:
        return model


async def refresh_chatgpt_subscription_for_admin(user: Any, model_id: uuid.UUID) -> ChatGPTSubscriptionRefreshOut:
    """Admin refresh of one company ChatGPT subscription row."""
    model = await llm_model_dao.get(model_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found")
    assert_can_manage_model(user, model)
    updated = await refresh_subscription_model(model)
    out = ChatGPTSubscriptionRefreshOut(ok=True, model_id=updated.id, expires_at=updated.token_expires_at)
    _assert_public(out)
    return out


async def probe_chatgpt_subscription(access_token: str) -> str:
    """Hit ChatGPT userinfo with the stored access token. Returns a short reply."""
    poll = await fetch_userinfo(access_token)
    if poll.status != "authorized":
        raise ValueError(poll.error or "ChatGPT subscription probe failed")
    return "ok"
