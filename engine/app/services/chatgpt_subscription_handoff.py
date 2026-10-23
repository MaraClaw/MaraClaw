"""Browser PKCE orchestration with single-use claims and short DB transactions."""

from datetime import UTC, datetime

import anyio
import httpx
from fastapi import HTTPException

from app.config import get_settings
from app.core.logging import logger
from app.core.security import decrypt_data, raise_if_password_change_required
from app.dao.llm_dao import llm_model_dao
from app.db.session import get_connection, optional_connection_ctx
from app.records.user import UserRecord
from app.services import chatgpt_subscription_store as store
from app.services.chatgpt_oauth import OPENAI_REDIRECT_URI, exchange_authorization_code
from app.services.chatgpt_pkce import BrowserCallback, new_browser_authorization, parse_browser_callback
from app.services.chatgpt_subscription import AUTH_KIND_CHATGPT_SUBSCRIPTION, persist_subscription_tokens
from app.services.chatgpt_subscription_types import (
    ChatGPTSubscriptionCompleteIn,
    ChatGPTSubscriptionStartOut,
    ChatGPTSubscriptionStatusOut,
)
from app.services.enterprise_llm import require_llm_pool_tenant_id
from app.services.subscription_boundaries import (
    active_subscription_tenant,
    fresh_subscription_actor,
    unchanged_subscription_model,
)


def _require_unbound() -> None:
    if get_connection() is not None:
        raise RuntimeError("ChatGPT browser handoff must own its transaction boundary")


async def _authorize(user: UserRecord, binding: store.AttemptBinding) -> None:
    fresh = await fresh_subscription_actor(user)
    binding.require_owner(fresh)
    if not fresh.is_active or fresh.identity is None or not fresh.identity.is_active:
        raise HTTPException(status_code=403, detail="Administrator identity is inactive")
    raise_if_password_change_required(fresh)
    require_llm_pool_tenant_id(fresh, str(binding.tenant_id))
    await active_subscription_tenant(binding.tenant_id)


def _expired(attempt_id: str) -> ChatGPTSubscriptionStatusOut:
    return ChatGPTSubscriptionStatusOut(status="expired", attempt_id=attempt_id, expires_in=0)


async def start_chatgpt_subscription_handoff(user: UserRecord, tenant_id: str | None) -> ChatGPTSubscriptionStartOut:
    _require_unbound()
    async with optional_connection_ctx():
        fresh = await fresh_subscription_actor(user)
        if not fresh.is_active or fresh.identity is None or not fresh.identity.is_active:
            raise HTTPException(status_code=403, detail="Administrator identity is inactive")
        raise_if_password_change_required(fresh)
        tid = require_llm_pool_tenant_id(fresh, tenant_id)
        await active_subscription_tenant(tid)
        binding = store.new_binding(fresh, tid)
    authorization = new_browser_authorization()
    attempt_id = await store.issue(binding, authorization)
    return ChatGPTSubscriptionStartOut(
        attempt_id=attempt_id,
        authorization_url=authorization.authorization_url,
        redirect_uri=OPENAI_REDIRECT_URI,
        expires_in=store.ATTEMPT_TTL,
    )


async def chatgpt_subscription_status(user: UserRecord, attempt_id: str) -> ChatGPTSubscriptionStatusOut:
    _require_unbound()
    view = await store.read_attempt(attempt_id, user)
    if view is None:
        return _expired(attempt_id)
    async with optional_connection_ctx():
        await _authorize(user, view.binding)
    if view.result is not None:
        return view.result
    remaining = max(int((view.binding.expires_at - datetime.now(UTC)).total_seconds()), 0)
    if remaining == 0:
        return _expired(attempt_id)
    return ChatGPTSubscriptionStatusOut(status="pending", attempt_id=attempt_id, expires_in=remaining)


async def complete_chatgpt_subscription_handoff(
    user: UserRecord, data: ChatGPTSubscriptionCompleteIn
) -> ChatGPTSubscriptionStatusOut:
    _require_unbound()
    view = await store.read_attempt(data.attempt_id, user)
    if view is None:
        return _expired(data.attempt_id)
    async with optional_connection_ctx():
        await _authorize(user, view.binding)
    if view.result is not None:
        raise HTTPException(status_code=409, detail="ChatGPT attempt already consumed; check status")
    if view.binding.expires_at <= datetime.now(UTC):
        return _expired(data.attempt_id)
    pending = await store.pending_ticket(data.attempt_id)
    if pending is None:
        raise HTTPException(status_code=409, detail="ChatGPT attempt already consumed; check status")
    if pending.binding != view.binding:
        raise store.unavailable()
    callback = parse_browser_callback(data.callback_url, pending.state)
    claimed = await store.claim(data.attempt_id, view.binding)
    if claimed is None:
        raise HTTPException(status_code=409, detail="ChatGPT attempt already consumed; check status")
    result = ChatGPTSubscriptionStatusOut(
        status="error", attempt_id=data.attempt_id, detail="ChatGPT sign-in did not complete; connect again"
    )
    try:
        if view.binding.expires_at <= datetime.now(UTC):
            result = _expired(data.attempt_id)
        else:
            result = await _exchange_and_persist(claimed, callback, user)
        return result
    finally:
        with anyio.CancelScope(shield=True):
            await store.finish(view.binding, result)


async def _exchange_and_persist(
    ticket: store.PendingTicket, callback: BrowserCallback, user: UserRecord
) -> ChatGPTSubscriptionStatusOut:
    attempt_id = ticket.attempt_id
    if callback.error is not None:
        return ChatGPTSubscriptionStatusOut(
            status="denied" if callback.error == "access_denied" else "error",
            attempt_id=attempt_id,
            detail="ChatGPT authorization was not approved; connect again",
        )
    if callback.code is None:
        raise ValueError("Missing authorization code")
    async with optional_connection_ctx():
        await _authorize(user, ticket.binding)
        snapshot = await llm_model_dao.get_subscription_for_tenant(
            ticket.binding.tenant_id, auth_kind=AUTH_KIND_CHATGPT_SUBSCRIPTION
        )
    try:
        verifier = decrypt_data(ticket.verifier_encrypted, get_settings().SECRET_KEY)
        token_result = await exchange_authorization_code(callback.code, verifier)
    except httpx.HTTPError, ValueError:
        logger.warning("ChatGPT browser token exchange failed")
        return ChatGPTSubscriptionStatusOut(
            status="error", attempt_id=attempt_id, detail="ChatGPT token exchange failed; connect again"
        )
    if token_result.status != "authorized" or token_result.tokens is None:
        return ChatGPTSubscriptionStatusOut(
            status=token_result.status if token_result.status != "authorized" else "error",
            attempt_id=attempt_id,
            detail=token_result.error or "ChatGPT token exchange failed",
        )
    async with optional_connection_ctx():
        await _authorize(user, ticket.binding)
        if ticket.binding.expires_at <= datetime.now(UTC):
            return _expired(attempt_id)
        if snapshot is not None:
            await unchanged_subscription_model(snapshot)
        current = await llm_model_dao.get_subscription_for_tenant(
            ticket.binding.tenant_id, auth_kind=AUTH_KIND_CHATGPT_SUBSCRIPTION
        )
        if current != snapshot:
            raise HTTPException(status_code=409, detail="Subscription changed during sign-in; connect again")
        saved = await persist_subscription_tokens(ticket.binding.tenant_id, token_result.tokens, existing=current)
    return ChatGPTSubscriptionStatusOut(status="authorized", attempt_id=attempt_id, model_id=saved.id)
