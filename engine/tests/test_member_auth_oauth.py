from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from tests.member_auth_fixtures import member_auth_setup, oauth_setup
from tests.oauth_state_fakes import REDIRECT_URI, exchange_mock, install_state_redis, sign_in_state

from app.api import auth, member_auth
from app.core.domain.users import UserRole
from app.services import oauth_state
from app.services.auth_audience import AuthAudience
from app.services.registration_service import registration_service
from app.services.sso_service import sso_service


@pytest.mark.parametrize("step", ["code", "pending"])
@pytest.mark.parametrize("operator", ["identity", "role"])
async def test_member_oauth_rejects_operator_before_link_cache_or_session(monkeypatch, step, operator):
    # Given an operator resolved by the real provider account flow.
    setup = member_auth_setup(monkeypatch, count=2)
    setup.identity.is_platform_admin = operator == "identity"
    if operator == "role":
        setup.users[-1].role = UserRole.PLATFORM_ADMIN
    stage, link = oauth_setup(monkeypatch, setup)
    payload = (
        {"state": await sign_in_state("feishu"), "code": "code"}
        if step == "code"
        else {"pending_token": "pending", "tenant_id": str(setup.tenants[0].id)}
    )
    # When either OAuth step is called through the member audience.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/feishu/callback", json=payload)
    # Then neither admissions, linking, pending credentials nor sessions are issued.
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "member_audience_rejected"
    stage.assert_not_awaited()
    link.assert_not_awaited()
    setup.pending.assert_not_awaited()
    setup.mint.assert_not_awaited()


@pytest.mark.parametrize("existing_user", [False, True])
@pytest.mark.parametrize("operator", ["identity", "role"])
async def test_member_oauth_checks_discovered_identity_before_registration_side_effects(
    monkeypatch,
    existing_user,
    operator,
):
    # Given a provider account whose global identity is discovered during registration.
    setup = member_auth_setup(monkeypatch, count=2)
    setup.identity.is_platform_admin = operator == "identity"
    if operator == "role":
        setup.users[-1].role = UserRole.PLATFORM_ADMIN
    setup.users[0].identity_id = None
    setup.users[0].identity = None
    stage, link = oauth_setup(monkeypatch, setup)
    if not existing_user:
        monkeypatch.setattr(sso_service, "resolve_user_identity", AsyncMock(return_value=None))
        monkeypatch.setattr(sso_service, "match_user_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(registration_service, "find_or_create_identity", AsyncMock(return_value=setup.identity))
    create_user = AsyncMock()
    monkeypatch.setattr("app.services.sso_company_admission.create_sso_user", create_user)
    # When OAuth registration identifies the platform account.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/feishu/callback", json={"state": await sign_in_state("feishu"), "code": "code"}
        )
    # Then admission, account creation, identity links and session issuance have not started.
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "member_audience_rejected"
    stage.assert_not_awaited()
    create_user.assert_not_awaited()
    link.assert_not_awaited()
    setup.mint.assert_not_awaited()


@pytest.mark.parametrize("step", ["code", "pending"])
async def test_member_oauth_projects_records_for_both_steps(monkeypatch, step):
    # Given a resolved eligible member account.
    setup = member_auth_setup(monkeypatch)
    oauth_setup(monkeypatch, setup)
    payload = (
        {"state": await sign_in_state("feishu"), "code": "code"}
        if step == "code"
        else {"pending_token": "pending", "tenant_id": str(setup.tenants[0].id)}
    )
    # When the provider callback succeeds.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/feishu/callback", json=payload)
    # Then the session carries a member-only projection.
    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(setup.users[0].id)
    assert "is_platform_admin" not in response.json()["user"]
    setup.mint.assert_awaited_once()


async def test_member_oauth_preserves_eligible_pending_tenant_selection(monkeypatch):
    # Given two eligible memberships.
    setup = member_auth_setup(monkeypatch, count=2)
    oauth_setup(monkeypatch, setup)
    # When the initial OAuth callback resolves the account.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/feishu/callback", json={"state": await sign_in_state("feishu"), "code": "code"}
        )
    # Then selection is cached without minting a session.
    assert response.status_code == 200
    assert response.json()["requires_tenant_selection"] is True
    assert len(response.json()["tenants"]) == 2
    assert response.json()["pending_token"]
    setup.pending.assert_awaited_once()
    setup.mint.assert_not_awaited()


@pytest.mark.parametrize("kind", ["forged", "replayed", "bind_intent", "other_provider", "expired"])
async def test_member_oauth_rejects_unusable_state_before_exchange_or_session(monkeypatch, kind):
    # Given a state that is forged, spent, minted for another intent or provider, or expired.
    setup = member_auth_setup(monkeypatch)
    oauth_setup(monkeypatch, setup)
    exchange = await exchange_mock("feishu")
    redis = install_state_redis(monkeypatch)
    state = {
        "forged": "attacker-chosen",
        "bind_intent": await oauth_state.issue("feishu", REDIRECT_URI, oauth_state.OAuthIntent.BIND),
        "other_provider": await oauth_state.issue("google", REDIRECT_URI, oauth_state.OAuthIntent.SIGN_IN),
    }.get(kind) or await sign_in_state("feishu")
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        if kind == "replayed":
            first = await client.post("/api/member/auth/feishu/callback", json={"state": state, "code": "code"})
            assert first.status_code == 200
            exchange.reset_mock()
            setup.mint.reset_mock()
        if kind == "expired":
            redis.expire_all()
        # When the code step presents it.
        response = await client.post("/api/member/auth/feishu/callback", json={"state": state, "code": "code"})
    # Then it is refused before the provider is contacted or a session minted.
    assert response.status_code == 400
    assert response.json()["detail"] == oauth_state.INVALID_STATE_DETAIL
    exchange.assert_not_awaited()
    setup.mint.assert_not_awaited()


async def test_member_oauth_requires_a_state_with_the_code(monkeypatch):
    # Given the code step without a state, as the pre-fix web-e client sent it.
    setup = member_auth_setup(monkeypatch)
    oauth_setup(monkeypatch, setup)
    exchange = await exchange_mock("feishu")
    # When it is posted.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/feishu/callback", json={"code": "code"})
    # Then it is a clean 400, not a schema failure, and nothing is exchanged.
    assert response.status_code == 400
    assert response.json()["detail"] == "Missing OAuth state"
    exchange.assert_not_awaited()


async def test_member_oauth_exchanges_with_the_stored_redirect_uri_not_the_requested_one(monkeypatch):
    # Given a state issued for one redirect URI and a callback that claims another.
    setup = member_auth_setup(monkeypatch)
    oauth_setup(monkeypatch, setup)
    exchange = await exchange_mock("feishu")
    state = await sign_in_state("feishu")
    payload = {"state": state, "code": "code", "redirect_uri": "https://evil.example/callback"}
    # When the code step succeeds.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/feishu/callback", json=payload)
    # Then the provider is called with the URI stored at authorize.
    assert response.status_code == 200
    exchange.assert_awaited_once_with("code", REDIRECT_URI)


async def test_member_oauth_fails_closed_when_state_storage_is_down(monkeypatch):
    # Given a state that was issued but Redis can no longer serve it.
    setup = member_auth_setup(monkeypatch)
    oauth_setup(monkeypatch, setup)
    exchange = await exchange_mock("feishu")
    redis = install_state_redis(monkeypatch)
    state = await sign_in_state("feishu")
    redis.fail_on.add("getdel")
    # When the code step arrives.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post("/api/member/auth/feishu/callback", json={"state": state, "code": "code"})
    # Then it is 503 with no exchange and no session.
    assert response.status_code == 503
    exchange.assert_not_awaited()
    setup.mint.assert_not_awaited()


@pytest.mark.parametrize("mismatch", ["provider", "audience"])
async def test_member_oauth_pending_token_is_bound_to_provider_and_audience(monkeypatch, mismatch):
    # Given a pending selection cached for another provider or for the shared audience.
    setup = member_auth_setup(monkeypatch, count=2)
    stage, link = oauth_setup(monkeypatch, setup)
    cached = auth.OAuthPendingCache(
        provider_type="feishu" if mismatch == "audience" else "google",
        audience=AuthAudience.SHARED if mismatch == "audience" else AuthAudience.MEMBER,
        user_info=auth.OAuthPendingUserInfo(provider_type="feishu", provider_user_id="remote-user", raw_data={}),
        token_data={},
        allowed_tenant_ids=[tenant.id for tenant in setup.tenants],
    )
    monkeypatch.setattr(auth, "_get_oauth_pending", AsyncMock(return_value=cached))
    # When it is redeemed through the member feishu callback.
    async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
        response = await client.post(
            "/api/member/auth/feishu/callback",
            json={"pending_token": "pending", "tenant_id": str(setup.tenants[0].id)},
        )
    # Then no account is resolved and no session minted.
    assert response.status_code == 400
    stage.assert_not_awaited()
    link.assert_not_awaited()
    setup.mint.assert_not_awaited()


async def test_member_bind_consumes_a_bind_state_once_and_exchanges_with_its_redirect_uri(monkeypatch):
    # Given a signed-in member with a bind state issued for the feishu provider.
    setup = member_auth_setup(monkeypatch)
    _, link = oauth_setup(monkeypatch, setup)
    monkeypatch.setattr(sso_service, "check_duplicate_identity", AsyncMock(return_value=None))
    setup.app.dependency_overrides[member_auth.get_current_member] = lambda: setup.users[0]
    exchange = await exchange_mock("feishu")
    state = await oauth_state.issue("feishu", REDIRECT_URI, oauth_state.OAuthIntent.BIND)
    payload = {"provider_type": "feishu", "code": "bind-code", "state": state}
    try:
        async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
            # When it binds, then replays the same state.
            first = await client.post("/api/member/auth/feishu/bind", json=payload)
            replay = await client.post("/api/member/auth/feishu/bind", json=payload)
    finally:
        setup.app.dependency_overrides.clear()
    # Then the first link succeeds with the stored redirect URI and the replay is refused.
    assert first.status_code == 200
    exchange.assert_awaited_once_with("bind-code", REDIRECT_URI)
    link.assert_awaited_once()
    assert replay.status_code == 400
    assert link.await_count == 1


@pytest.mark.parametrize("kind", ["sign_in_intent", "other_provider", "missing"])
async def test_member_bind_refuses_a_state_that_is_not_its_own_before_any_exchange(monkeypatch, kind):
    # Given a sign-in state, a state for another provider, or no state at all.
    setup = member_auth_setup(monkeypatch)
    _, link = oauth_setup(monkeypatch, setup)
    setup.app.dependency_overrides[member_auth.get_current_member] = lambda: setup.users[0]
    exchange = await exchange_mock("feishu")
    payload: dict[str, str] = {"provider_type": "feishu", "code": "bind-code"}
    if kind == "sign_in_intent":
        payload["state"] = await sign_in_state("feishu")
    elif kind == "other_provider":
        payload["state"] = await oauth_state.issue("google", REDIRECT_URI, oauth_state.OAuthIntent.BIND)
    # When the member tries to bind with it.
    try:
        async with AsyncClient(transport=ASGITransport(app=setup.app), base_url="http://test") as client:
            response = await client.post("/api/member/auth/feishu/bind", json=payload)
    finally:
        setup.app.dependency_overrides.clear()
    # Then nothing is exchanged or linked.
    assert response.status_code == (422 if kind == "missing" else 400)
    exchange.assert_not_awaited()
    link.assert_not_awaited()
