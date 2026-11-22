from dataclasses import replace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.chatgpt_subscription_fakes import CLIENT_ID, REDIRECT_URI, callback_query, oauth_store, state_of

__all__ = ["oauth_store"]
from tests.test_chatgpt_subscription import _ACCESS, FakeChatGPTTransport, _assert_no_secrets, _model, _user

from app.config import get_settings
from app.core.domain.users import UserRole
from app.core.security import decrypt_data
from app.services import (
    chatgpt_oauth as oauth,
    chatgpt_subscription as persistence,
    chatgpt_subscription_handoff as browser,
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
        ticket = oauth_store.pending(start.attempt_id)
        assert ticket is not None
        verifier = decrypt_data(ticket.verifier_encrypted, get_settings().SECRET_KEY)
        assert 43 <= len(verifier) <= 128
        assert verifier not in str(oauth_store.rows)
        assert not any(ticket.state in key for key in oauth_store.rows)
        assert query == {
            "client_id": [CLIENT_ID],
            "redirect_uri": [REDIRECT_URI],
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
        assert set(start.model_dump()) == {"attempt_id", "authorization_url", "expires_in"}
        assert start.expires_in == 600
        states.add(ticket.state)
        challenges.add(query["code_challenge"][0])
    assert len(states) == len(challenges) == 2
    for kind in ("pending", "state"):
        assert [ex for key, ex, nx in oauth_store.writes if key.endswith(f":{kind}") and nx] == [600, 600]
    assert transport.calls == []


async def test_start_is_unavailable_without_a_configured_redirect(monkeypatch, oauth_store):
    # Given
    monkeypatch.setenv("CHATGPT_OAUTH_REDIRECT_URI", "")
    get_settings.cache_clear()
    # When
    with pytest.raises(HTTPException) as exc:
        await browser.start_chatgpt_subscription_handoff(_user(), None)
    # Then
    assert exc.value.status_code == 503
    assert oauth_store.rows == {}


@pytest.mark.parametrize("failure", ["state", "duplicate", "blank", "both"])
async def test_bad_callback_is_retryable_without_consuming_attempt(oauth_store, failure):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    valid = callback_query(start, error="access_denied")
    bad = {
        "state": urlencode({"state": "wrong", "error": "access_denied"}),
        "duplicate": valid + "&error=access_denied",
        "blank": urlencode({"state": state_of(start), "code": " "}),
        "both": valid + "&code=authorization-code",
    }[failure]
    # When
    rejected = await browser.complete_chatgpt_subscription_callback(bad)
    intact = oauth_store.pending(start.attempt_id)
    settled = await browser.complete_chatgpt_subscription_callback(valid)
    result = await browser.chatgpt_subscription_status(user, start.attempt_id)
    # Then
    assert rejected == settled == "rejected"
    assert intact is not None
    assert result.status == "denied"


@pytest.mark.parametrize("owner_change", ["actor", "identity", "tenant"])
@pytest.mark.parametrize("terminal", [False, True])
async def test_attempt_and_recovery_results_are_bound_to_full_owner(oauth_store, owner_change, terminal):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    if terminal:
        await browser.complete_chatgpt_subscription_callback(callback_query(start, error="access_denied"))
    intruder = replace(
        user, **{{"actor": "id", "identity": "identity_id", "tenant": "tenant_id"}[owner_change]: uuid4()}
    )
    # When
    with pytest.raises(HTTPException) as exc:
        await browser.chatgpt_subscription_status(intruder, start.attempt_id)
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
    late = await browser.complete_chatgpt_subscription_callback(callback_query(start))
    expired = await browser.chatgpt_subscription_status(user, start.attempt_id)
    unknown = await browser.chatgpt_subscription_status(user, "unknown")
    # Then
    assert pending.status == "pending"
    assert pending.expires_in == 600
    assert late == "rejected"
    assert expired.status == unknown.status == "expired"
    assert expired.expires_in == unknown.expires_in == 0
    assert transport.calls == []


@pytest.mark.parametrize(("error", "status"), [("access_denied", "denied"), ("temporarily_unavailable", "error")])
async def test_provider_refusal_has_sanitized_recoverable_result_and_replay_is_rejected(oauth_store, error, status):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    query = callback_query(start, error=error, error_description="private provider prose")
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    # When
    settled = await browser.complete_chatgpt_subscription_callback(query)
    recovered = await browser.chatgpt_subscription_status(user, start.attempt_id)
    replay = await browser.complete_chatgpt_subscription_callback(query)
    # Then
    assert settled == replay == "rejected"
    assert recovered.status == status
    assert "private provider prose" not in recovered.model_dump_json()
    assert "private provider prose" not in str(oauth_store.rows)
    assert transport.calls == []
    assert [ex for key, ex, nx in oauth_store.writes if key.endswith(":result") and nx] == [300]


async def test_callback_encrypts_credentials_and_preserves_codex_pool_activation(monkeypatch, oauth_store):
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
    ticket = oauth_store.pending(start.attempt_id)
    assert ticket is not None
    # When
    settled = await browser.complete_chatgpt_subscription_callback(callback_query(start))
    result = await browser.chatgpt_subscription_status(user, start.attempt_id)
    # Then
    assert settled == "authorized"
    assert result.status == "authorized"
    assert result.model_id == created.id
    _assert_no_secrets(result.model_dump())
    assert _ACCESS not in str(oauth_store.rows)
    assert create.await_args is not None
    fields = create.await_args.kwargs["obj_in"]
    assert decrypt_data(fields["api_key_encrypted"], get_settings().SECRET_KEY) == _ACCESS
    assert decrypt_data(fields["refresh_token_encrypted"], get_settings().SECRET_KEY) == "oa-refresh-LIVE"
    assert fields["tenant_id"] == user.tenant_id
    assert fields["auth_kind"] == "chatgpt_subscription"
    assert fields["provider"] == "openai"
    assert fields["model"] == "gpt-6.2-sol"
    assert fields["reasoning_effort"] == "high"
    assert fields["supports_vision"] is True
    assert fields["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert fields["oauth_account_id"] == "acct-live"
    activate.assert_awaited_once_with(created)
    assert transport.calls == [
        (
            "https://auth.openai.com/oauth/token",
            "form",
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": "authorization-code",
                "redirect_uri": REDIRECT_URI,
                "code_verifier": decrypt_data(ticket.verifier_encrypted, get_settings().SECRET_KEY),
            },
        )
    ]


async def test_refresh_uses_the_configured_client(oauth_store):
    # Given
    transport = FakeChatGPTTransport()
    # When
    refreshed = await oauth.refresh_access_token("stored-refresh", transport=transport)
    # Then
    assert refreshed.status == "authorized"
    assert [form["client_id"] for _, _, form in transport.calls] == [CLIENT_ID]


async def test_concurrent_callbacks_permit_only_one_exchange(monkeypatch, oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    entered, release = anyio.Event(), anyio.Event()
    outcomes = []
    replays = []
    saved = _model()

    async def remote(*_args):
        entered.set()
        await release.wait()
        return oauth.interpret_token_response(200, {"access_token": _ACCESS, "refresh_token": "refresh"})

    exchange = AsyncMock(side_effect=remote)
    monkeypatch.setattr(browser, "exchange_authorization_code", exchange)
    monkeypatch.setattr(browser, "persist_subscription_tokens", AsyncMock(return_value=saved))

    async def complete():
        outcomes.append(await browser.complete_chatgpt_subscription_callback(callback_query(start)))

    # When
    with anyio.fail_after(3):
        async with anyio.create_task_group() as group:
            group.start_soon(complete)
            await entered.wait()
            try:
                replays.append(await browser.complete_chatgpt_subscription_callback(callback_query(start)))
            finally:
                release.set()
    # Then
    assert replays == ["rejected"]
    assert outcomes == ["authorized"]
    exchange.assert_awaited_once()


@pytest.mark.parametrize("change", ["inactive", "identity", "password", "role", "moved", "removed"])
async def test_owner_authority_is_rechecked_freshly_at_callback(monkeypatch, oauth_store, change):
    # Given
    user = _user()
    assert user.identity is not None
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    changed = {
        "inactive": replace(user, is_active=False),
        "identity": replace(user, identity=replace(user.identity, is_active=False)),
        "password": replace(user, identity=replace(user.identity, must_change_password=True)),
        "role": replace(user, role=UserRole("member")),
        "moved": replace(user, tenant_id=uuid4()),
        "removed": None,
    }[change]
    if changed is None:
        del oauth_store.accounts[user.id]
    else:
        oauth_store.accounts[user.id] = changed
    exchange = AsyncMock()
    monkeypatch.setattr(browser, "exchange_authorization_code", exchange)
    # When
    settled = await browser.complete_chatgpt_subscription_callback(callback_query(start))
    result = await browser.chatgpt_subscription_status(user, start.attempt_id)
    # Then
    assert settled == "rejected"
    assert result.status == "error"
    exchange.assert_not_awaited()
    assert oauth_store.pending(start.attempt_id) is None
