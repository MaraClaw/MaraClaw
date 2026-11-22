"""Browser PKCE orchestration with single-use claims and short DB transactions.

The admin starts an attempt and polls ``status``; the provider redirects the browser to the engine-hosted callback
(``app/api/chatgpt_oauth_callback.py``), which carries no session, so the claimed ticket's binding names the owner.
"""

from datetime import UTC, datetime
from typing import Literal

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
from app.services.chatgpt_oauth import exchange_authorization_code
from app.services.chatgpt_pkce import BrowserCallback, new_browser_authorization, parse_callback_query
from app.services.chatgpt_subscription import AUTH_KIND_CHATGPT_SUBSCRIPTION, persist_subscription_tokens
from app.services.chatgpt_subscription_types import ChatGPTSubscriptionStartOut, ChatGPTSubscriptionStatusOut
from app.services.enterprise_llm import require_llm_pool_tenant_id
from app.services.subscription_boundaries import (
    active_subscription_tenant,
    fresh_subscription_actor,
    fresh_subscription_owner,
    unchanged_subscription_model,
)

CallbackOutcome = Literal["authorized", "rejected", "unavailable"]

_NOT_COMPLETED = "ChatGPT sign-in did not complete; connect again"
_SUBSCRIPTION_CHANGED = "Subscription changed during sign-in; connect again"


def _require_unbound() -> None:
    if get_connection() is not None:
        raise RuntimeError("ChatGPT browser handoff must own its transaction boundary")


async def _authorize(binding: store.AttemptBinding, user: UserRecord | None = None) -> None:
    """Fresh authority of the attempt owner: the session user, or the recorded actor on the browser callback."""
    if user is None:
        fresh = await fresh_subscription_owner(binding.actor_id, binding.identity_id, binding.actor_tenant_id)
    else:
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
    if not get_settings().CHATGPT_OAUTH_REDIRECT_URI:
        raise HTTPException(status_code=503, detail="ChatGPT sign-in is not configured")
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
        attempt_id=attempt_id, authorization_url=authorization.authorization_url, expires_in=store.ATTEMPT_TTL
    )


async def chatgpt_subscription_status(user: UserRecord, attempt_id: str) -> ChatGPTSubscriptionStatusOut:
    _require_unbound()
    view = await store.read_attempt(attempt_id, user)
    if view is None:
        return _expired(attempt_id)
    async with optional_connection_ctx():
        await _authorize(view.binding, user)
    if view.result is not None:
        return view.result
    remaining = max(int((view.binding.expires_at - datetime.now(UTC)).total_seconds()), 0)
    if remaining == 0:
        return _expired(attempt_id)
    return ChatGPTSubscriptionStatusOut(status="pending", attempt_id=attempt_id, expires_in=remaining)


async def complete_chatgpt_subscription_callback(query: str) -> CallbackOutcome:
    """Settle the attempt named by the raw redirect query; the owner reads the stored result through status.

    A malformed query or an unknown, expired or used state is ``rejected`` without consuming anything. Every
    terminal result other than ``authorized`` is also ``rejected``, so the page never tells whether a state was valid.
    """
    _require_unbound()
    callback = parse_callback_query(query)
    if callback is None:
        return "rejected"
    try:
        result = await _consume(callback)
    except HTTPException as exc:
        if exc.status_code == 503:
            return "unavailable"
        raise
    return "authorized" if result is not None and result.status == "authorized" else "rejected"


async def _consume(callback: BrowserCallback) -> ChatGPTSubscriptionStatusOut | None:
    ticket = await store.claim_by_state(callback.state)
    if ticket is None:
        return None
    result = ChatGPTSubscriptionStatusOut(status="error", attempt_id=ticket.attempt_id, detail=_NOT_COMPLETED)
    try:
        result = await _settle(ticket, callback)
        return result
    finally:
        with anyio.CancelScope(shield=True):
            await store.finish(ticket.binding, result)


async def _settle(ticket: store.PendingTicket, callback: BrowserCallback) -> ChatGPTSubscriptionStatusOut:
    if ticket.binding.expires_at <= datetime.now(UTC):
        return _expired(ticket.attempt_id)
    try:
        return await _exchange_and_persist(ticket, callback)
    except HTTPException as exc:
        # Authority or the stored subscription changed after start; nothing was persisted.
        logger.warning("ChatGPT browser callback refused: status_code={}", exc.status_code)
        detail = _SUBSCRIPTION_CHANGED if exc.status_code in {404, 409} else _NOT_COMPLETED
        return ChatGPTSubscriptionStatusOut(status="error", attempt_id=ticket.attempt_id, detail=detail)


async def _exchange_and_persist(ticket: store.PendingTicket, callback: BrowserCallback) -> ChatGPTSubscriptionStatusOut:
    attempt_id = ticket.attempt_id
    async with optional_connection_ctx():
        await _authorize(ticket.binding)
        snapshot = await llm_model_dao.get_subscription_for_tenant(
            ticket.binding.tenant_id, auth_kind=AUTH_KIND_CHATGPT_SUBSCRIPTION
        )
    if callback.code is None:
        return ChatGPTSubscriptionStatusOut(
            status="denied" if callback.error == "access_denied" else "error",
            attempt_id=attempt_id,
            detail="ChatGPT authorization was not approved; connect again",
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
        await _authorize(ticket.binding)
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
