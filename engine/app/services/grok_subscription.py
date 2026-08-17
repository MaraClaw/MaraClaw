"""Company-level Grok SuperGrok / X Premium handoff into the LLM pool.

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
from app.services.enterprise_llm import (
    activate_pool_model_for_tenant,
    assert_can_manage_model,
    require_llm_pool_tenant_id,
)
from app.services.grok_oauth import (
    DeviceCodeChallenge,
    GrokOAuthTokens,
    poll_device_token,
    refresh_access_token,
    request_device_code,
)
from app.services.llm import get_model_api_key

AUTH_KIND_API_KEY: Final = "api_key"
AUTH_KIND_GROK_SUBSCRIPTION: Final = "grok_subscription"
GROK_SUBSCRIPTION_PROVIDER: Final = "grok"
GROK_SUBSCRIPTION_MODEL: Final = "grok-4.6"
GROK_SUBSCRIPTION_LABEL: Final = "Grok SuperGrok"
GROK_SUBSCRIPTION_BASE_URL: Final = "https://api.x.ai/v1"
REFRESH_SKEW = timedelta(minutes=5)

GrokStatus = Literal["pending", "authorized", "expired", "denied", "error"]


class GrokSubscriptionStartOut(BaseModel):
    """Human-facing start payload. Never includes tokens or device_code."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    session_id: str
    verification_url: str
    user_code: str
    expires_in: int
    interval: int


class GrokSubscriptionStatusOut(BaseModel):
    """Human-facing poll payload. Tokens stay on the server."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    status: GrokStatus
    session_id: str
    verification_url: str | None = None
    user_code: str | None = None
    model_id: uuid.UUID | None = None
    detail: str | None = None
    interval: int | None = None


class GrokSubscriptionRefreshOut(BaseModel):
    """Refresh acknowledgement. No token fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    ok: bool
    model_id: uuid.UUID
    expires_at: datetime | None = None


@dataclass(slots=True)
class GrokOAuthSession:
    """In-process pending device-code session. device_code never leaves this module."""

    session_id: str
    tenant_id: uuid.UUID
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_at: datetime
    status: GrokStatus = "pending"
    model_id: uuid.UUID | None = None
    detail: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


_SESSIONS: dict[str, GrokOAuthSession] = {}

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "device_code",
        "cookie",
        "cookies",
        "password",
        "api_key",
        "token",
    }
)


def reset_grok_sessions() -> None:
    """Drop pending sessions. Tests call this between cases."""
    _SESSIONS.clear()


def _assert_public(payload: BaseModel) -> None:
    dumped = payload.model_dump()
    leaked = _SECRET_KEYS.intersection(dumped)
    if leaked:
        raise RuntimeError(f"refusing to expose secret fields: {sorted(leaked)}")


def public_start_payload(session: GrokOAuthSession) -> GrokSubscriptionStartOut:
    remaining = max(int((session.expires_at - datetime.now(UTC)).total_seconds()), 0)
    out = GrokSubscriptionStartOut(
        session_id=session.session_id,
        verification_url=session.verification_url,
        user_code=session.user_code,
        expires_in=remaining,
        interval=session.interval,
    )
    _assert_public(out)
    return out


def public_status_payload(session: GrokOAuthSession) -> GrokSubscriptionStatusOut:
    out = GrokSubscriptionStatusOut(
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


def _store_session(challenge: DeviceCodeChallenge, tenant_id: uuid.UUID) -> GrokOAuthSession:
    session = GrokOAuthSession(
        session_id=secrets.token_urlsafe(32),
        tenant_id=tenant_id,
        device_code=challenge.device_code,
        user_code=challenge.user_code,
        verification_url=challenge.verification_url,
        interval=challenge.interval,
        expires_at=datetime.now(UTC) + timedelta(seconds=challenge.expires_in),
    )
    _SESSIONS[session.session_id] = session
    return session


def _get_session(session_id: str) -> GrokOAuthSession:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown Grok subscription session")
    return session


def _require_session_tenant(user: Any, session: GrokOAuthSession) -> uuid.UUID:
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
    tokens: GrokOAuthTokens,
    *,
    existing: LLMModelRecord | None = None,
) -> LLMModelRecord:
    """Upsert the company Grok subscription pool row. Access + refresh stay encrypted."""
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens.expires_in)
    updates: dict[str, object] = {
        "api_key_encrypted": _encrypt(tokens.access_token),
        "token_expires_at": expires_at,
        "auth_kind": AUTH_KIND_GROK_SUBSCRIPTION,
        "enabled": True,
        "provider": GROK_SUBSCRIPTION_PROVIDER,
        "model": GROK_SUBSCRIPTION_MODEL,
        "base_url": GROK_SUBSCRIPTION_BASE_URL,
    }
    if tokens.refresh_token:
        updates["refresh_token_encrypted"] = _encrypt(tokens.refresh_token)
    row = existing or await llm_model_dao.get_subscription_for_tenant(tenant_id)
    if row is not None:
        saved = await llm_model_dao.update(db_obj=row, obj_in=updates)
    else:
        saved = await llm_model_dao.create(
            obj_in={
                **updates,
                "label": GROK_SUBSCRIPTION_LABEL,
                "tenant_id": tenant_id,
                "supports_vision": True,
            }
        )
    await activate_pool_model_for_tenant(saved)
    return saved


async def start_grok_subscription_handoff(user: Any, tenant_id: str | None) -> GrokSubscriptionStartOut:
    """Admin-only device-code start. Invokes the shipped xAI start path."""
    tid = require_llm_pool_tenant_id(user, tenant_id)
    challenge = await request_device_code()
    session = _store_session(challenge, tid)
    return public_start_payload(session)


async def grok_subscription_status(user: Any, session_id: str) -> GrokSubscriptionStatusOut:
    """Poll xAI once and, on success, persist an encrypted company pool row."""
    session = _get_session(session_id)
    _require_session_tenant(user, session)
    if session.status != "pending":
        return public_status_payload(session)
    if datetime.now(UTC) >= session.expires_at:
        session.status = "expired"
        session.detail = "Device-code sign-in expired"
        session.device_code = ""
        return public_status_payload(session)

    poll = await poll_device_token(session.device_code, interval=session.interval)
    if poll.interval:
        session.interval = poll.interval
    if poll.status == "pending":
        return public_status_payload(session)
    if poll.status == "authorized" and poll.tokens is not None:
        row = await persist_subscription_tokens(session.tenant_id, poll.tokens)
        session.status = "authorized"
        session.model_id = row.id
        session.detail = "Grok subscription connected"
        session.device_code = ""
        return public_status_payload(session)
    next_status: GrokStatus = cast(GrokStatus, poll.status if poll.status in {"denied", "expired", "error"} else "error")
    session.status = next_status
    session.detail = poll.error or "Grok subscription sign-in failed"
    session.device_code = ""
    return public_status_payload(session)


async def refresh_subscription_model(model: LLMModelRecord) -> LLMModelRecord:
    """Refresh a stored subscription without a new browser login. Shipped refresh path."""
    kind = getattr(model, "auth_kind", AUTH_KIND_API_KEY) or AUTH_KIND_API_KEY
    if kind != AUTH_KIND_GROK_SUBSCRIPTION:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a Grok subscription model")
    refresh = _decrypt(getattr(model, "refresh_token_encrypted", None))
    if not refresh:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Grok subscription has no refresh token; connect again",
        )
    poll = await refresh_access_token(refresh)
    if poll.status != "authorized" or poll.tokens is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=poll.error or "Grok subscription refresh failed",
        )
    tokens = poll.tokens
    if not tokens.refresh_token:
        tokens = GrokOAuthTokens(
            access_token=tokens.access_token,
            refresh_token=refresh,
            expires_in=tokens.expires_in,
            token_type=tokens.token_type,
            scope=tokens.scope,
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
    if kind != AUTH_KIND_GROK_SUBSCRIPTION:
        return model
    if _is_fresh(model) and get_model_api_key(model):
        return model
    try:
        return await refresh_subscription_model(model)
    except HTTPException:
        return model


async def refresh_grok_subscription_for_admin(user: Any, model_id: uuid.UUID) -> GrokSubscriptionRefreshOut:
    """Admin refresh of one company Grok subscription row."""
    model = await llm_model_dao.get(model_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found")
    assert_can_manage_model(user, model)
    updated = await refresh_subscription_model(model)
    out = GrokSubscriptionRefreshOut(ok=True, model_id=updated.id, expires_at=updated.token_expires_at)
    _assert_public(out)
    return out
