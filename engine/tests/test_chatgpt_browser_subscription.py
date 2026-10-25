from dataclasses import replace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.chatgpt_subscription_fakes import callback, oauth_store

__all__ = ["oauth_store"]
from tests.test_chatgpt_subscription import _ACCESS, FakeChatGPTTransport, _assert_no_secrets, _model, _user

from app.config import get_settings
from app.core.security import decrypt_data
from app.services import (
    chatgpt_oauth as oauth,
    chatgpt_subscription as persistence,
    chatgpt_subscription_handoff as browser,
    chatgpt_subscription_store as store,
)
from app.services.chatgpt_pkce import pkce_challenge


async def test_start_generates_unique_pkce_and_keeps_only_encrypted_private_verifier(oauth_store):
    # Given
    user = _user()
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    # When
    starts = [await browser.start_chatgpt_subscription_handoff(user, None) for _ in range(2)]
    # Then
    assert starts[0].attempt_id != starts[1].attempt_id
    states, challenges = set(), set()
    for start in starts:
        url = urlsplit(start.authorization_url)
        query = parse_qs(url.query)
        ticket = await store.pending_ticket(start.attempt_id)
        assert ticket is not None
        verifier = decrypt_data(ticket.verifier_encrypted, get_settings().SECRET_KEY)
        assert 43 <= len(verifier) <= 128
        assert verifier not in str(oauth_store.rows)
        assert query == {
            "client_id": ["app_EMoamEEZ73f0CkXaXp7hrann"],
            "redirect_uri": ["http://localhost:1455/auth/callback"],
            "scope": ["openid profile email offline_access"],
            "response_type": ["code"],
            "code_challenge": [pkce_challenge(verifier)],
            "code_challenge_method": ["S256"],
            "state": [ticket.state],
            "id_token_add_organizations": ["true"],
            "codex_cli_simplified_flow": ["true"],
            "originator": ["maraclaw"],
        }
        assert (url.scheme, url.netloc, url.path) == ("https", "auth.openai.com", "/oauth/authorize")
        assert start.redirect_uri == query["redirect_uri"][0]
        assert start.expires_in == 600
        states.add(ticket.state)
        challenges.add(query["code_challenge"][0])
    assert len(states) == len(challenges) == 2
    assert [ex for key, ex, nx in oauth_store.writes if key.endswith(":pending") and nx] == [600, 600]
    assert transport.calls == []


@pytest.mark.parametrize("failure", ["state", "duplicate", "origin"])
async def test_bad_callback_is_retryable_without_consuming_attempt(oauth_store, failure):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    data = callback(start, error="access_denied")
    wrong = {
        "state": data.callback_url.replace("state=", "state=wrong"),
        "duplicate": data.callback_url + "&error=access_denied",
        "origin": data.callback_url.replace("localhost", "127.0.0.1"),
    }[failure]
    # When
    with pytest.raises(HTTPException) as exc:
        await browser.complete_chatgpt_subscription_handoff(user, data.model_copy(update={"callback_url": wrong}))
    result = await browser.complete_chatgpt_subscription_handoff(user, data)
    # Then
    assert exc.value.status_code == 400
    assert result.status == "denied"


@pytest.mark.parametrize("owner_change", ["actor", "identity", "tenant"])
@pytest.mark.parametrize("terminal", [False, True])
async def test_attempt_and_recovery_results_are_bound_to_full_owner(oauth_store, owner_change, terminal):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    data = callback(start, error="access_denied")
    if terminal:
        await browser.complete_chatgpt_subscription_handoff(user, data)
    intruder = replace(
        user, **{{"actor": "id", "identity": "identity_id", "tenant": "tenant_id"}[owner_change]: uuid4()}
    )
    # When
    for action in (
        browser.chatgpt_subscription_status(intruder, start.attempt_id),
        browser.complete_chatgpt_subscription_handoff(intruder, data),
    ):
        with pytest.raises(HTTPException) as exc:
            await action
        # Then
        assert exc.value.status_code == 404


async def test_status_is_read_only_and_expiry_matches_unknown_attempt(oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    before = dict(oauth_store.rows)
    # When
    pending = await browser.chatgpt_subscription_status(user, start.attempt_id)
    assert oauth_store.rows == before
    oauth_store.clock.advance(600)
    expired = await browser.complete_chatgpt_subscription_handoff(user, callback(start))
    unknown = await browser.chatgpt_subscription_status(user, "unknown")
    # Then
    assert pending.status == "pending"
    assert pending.expires_in == 600
    assert expired.status == unknown.status == "expired"
    assert expired.expires_in == unknown.expires_in == 0
    assert transport.calls == []


async def test_denial_has_sanitized_recoverable_result_and_replay_conflict(oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    data = callback(start, error="access_denied", error_description="private provider prose")
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    # When
    result = await browser.complete_chatgpt_subscription_handoff(user, data)
    recovered = await browser.chatgpt_subscription_status(user, start.attempt_id)
    with pytest.raises(HTTPException) as exc:
        await browser.complete_chatgpt_subscription_handoff(user, data)
    # Then
    assert recovered == result
    assert result.status == "denied"
    assert "private provider prose" not in result.model_dump_json()
    assert exc.value.status_code == 409
    assert transport.calls == []
    assert [ex for key, ex, nx in oauth_store.writes if key.endswith(":result") and nx] == [300]


async def test_complete_encrypts_credentials_and_preserves_codex_pool_activation(monkeypatch, oauth_store):
    # Given
    user = _user()
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    created = _model()
    create = AsyncMock(return_value=created)
    activate = AsyncMock()
    monkeypatch.setattr(persistence.llm_model_dao, "create", create)
    monkeypatch.setattr(persistence, "activate_pool_model_for_tenant", activate)
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    ticket = await store.pending_ticket(start.attempt_id)
    assert ticket is not None
    # When
    result = await browser.complete_chatgpt_subscription_handoff(user, callback(start))
    # Then
    assert result.status == "authorized"
    assert result.model_id == created.id
    _assert_no_secrets(result.model_dump())
    assert create.await_args is not None
    fields = create.await_args.kwargs["obj_in"]
    assert decrypt_data(fields["api_key_encrypted"], get_settings().SECRET_KEY) == _ACCESS
    assert decrypt_data(fields["refresh_token_encrypted"], get_settings().SECRET_KEY) == "oa-refresh-LIVE"
    assert fields["tenant_id"] == user.tenant_id
    assert fields["auth_kind"] == "chatgpt_subscription"
    assert fields["provider"] == "openai"
    assert fields["model"]
    assert fields["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert fields["oauth_account_id"] == "acct-live"
    activate.assert_awaited_once_with(created)
    assert transport.calls == [
        (
            "https://auth.openai.com/oauth/token",
            "form",
            {
                "grant_type": "authorization_code",
                "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                "code": "authorization-code",
                "redirect_uri": "http://localhost:1455/auth/callback",
                "code_verifier": decrypt_data(ticket.verifier_encrypted, get_settings().SECRET_KEY),
            },
        )
    ]


async def test_concurrent_completion_permits_only_one_exchange(monkeypatch, oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    entered, release = anyio.Event(), anyio.Event()
    responses = []
    conflicts = []
    saved = _model()

    async def remote(*_args):
        entered.set()
        await release.wait()
        return oauth.interpret_token_response(200, {"access_token": _ACCESS, "refresh_token": "refresh"})

    exchange = AsyncMock(side_effect=remote)
    monkeypatch.setattr(browser, "exchange_authorization_code", exchange)
    monkeypatch.setattr(browser, "persist_subscription_tokens", AsyncMock(return_value=saved))

    async def complete():
        responses.append(await browser.complete_chatgpt_subscription_handoff(user, callback(start)))

    # When
    with anyio.fail_after(3):
        async with anyio.create_task_group() as group:
            group.start_soon(complete)
            await entered.wait()
            try:
                with pytest.raises(HTTPException) as exc:
                    await browser.complete_chatgpt_subscription_handoff(user, callback(start))
                conflicts.append(exc.value.status_code)
            finally:
                release.set()
    # Then
    assert conflicts == [409]
    assert responses[0].status == "authorized"
    exchange.assert_awaited_once()
