import base64
import hashlib
import re
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from tests.chatgpt_subscription_fakes import CALLBACK_PATH, CLIENT_ID, REDIRECT_URI, callback_query, oauth_store

from route_dispatch import effective_routes

__all__ = ["oauth_store"]
from tests.test_chatgpt_subscription import FakeChatGPTTransport, _assert_no_secrets, _model, _user

from app.api import enterprise
from app.api.chatgpt_oauth_callback import build_callback_router
from app.config import get_settings
from app.records.user import UserRecord
from app.services import chatgpt_oauth as oauth, chatgpt_subscription_handoff as browser
from app.services.chatgpt_subscription_types import ChatGPTSubscriptionStartOut

ROOT = "/api/enterprise/llm-models/chatgpt-subscription"


def _app(user: UserRecord) -> FastAPI:
    # The callback router reads the redirect URI when built, as app.main does once at import.
    app = FastAPI()
    app.include_router(enterprise.router, prefix="/api")
    app.include_router(build_callback_router())
    app.dependency_overrides[enterprise.get_current_admin] = lambda: user
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _assert_isolated_page(response: httpx.Response) -> None:
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    policy = response.headers["content-security-policy"].split("; ")
    directives = {name: value for name, _, value in (directive.partition(" ") for directive in policy)}
    assert directives["default-src"] == "'none'"
    styles = re.findall(r"<style>(.*?)</style>", response.text)
    hashes = {f"'sha256-{base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()}'" for style in styles}
    assert set(directives.get("style-src", "").split()) == hashes
    assert not re.search(r"<script|\bsrc=|\bhref=", response.text)


async def test_http_start_callback_and_read_only_status_contract(monkeypatch, oauth_store):
    # Given
    user = _user()
    app = _app(user)
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    monkeypatch.setattr(browser, "persist_subscription_tokens", AsyncMock(return_value=_model()))
    try:
        # When
        async with _client(app) as client:
            started = await client.post(ROOT + "/start")
            assert started.status_code == 200
            start = ChatGPTSubscriptionStartOut.model_validate(started.json())
            pending = await client.get(ROOT + "/status", params={"attempt_id": start.attempt_id})
            query = callback_query(start)
            finished = await client.get(f"{CALLBACK_PATH}?{query}")
            recovered = await client.get(ROOT + "/status", params={"attempt_id": start.attempt_id})
            replay = await client.get(f"{CALLBACK_PATH}?{query}")
            removed = await client.post(ROOT + "/complete", json={"attempt_id": start.attempt_id})
    finally:
        app.dependency_overrides.clear()
    # Then
    authorize = parse_qs(urlsplit(start.authorization_url).query)
    assert set(started.json()) == {"attempt_id", "authorization_url", "expires_in"}
    assert (authorize["client_id"], authorize["redirect_uri"]) == ([CLIENT_ID], [REDIRECT_URI])
    assert pending.json()["status"] == "pending"
    assert finished.status_code == 200
    _assert_isolated_page(finished)
    assert authorize["state"][0] not in finished.text
    assert "authorization-code" not in finished.text
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "authorized"
    assert set(recovered.json()) == {"status", "attempt_id", "model_id", "detail", "expires_in"}
    _assert_no_secrets(recovered.json())
    assert replay.status_code == 400
    assert removed.status_code in {404, 405}
    assert [(form["client_id"], form["redirect_uri"]) for _, _, form in transport.calls] == [(CLIENT_ID, REDIRECT_URI)]


async def test_every_failed_callback_answers_the_same_isolated_page(oauth_store):
    # Given
    user = _user()
    app = _app(user)
    try:
        async with _client(app) as client:
            started = await client.post(ROOT + "/start")
            start = ChatGPTSubscriptionStartOut.model_validate(started.json())
            denial = callback_query(start, error="access_denied", error_description="private provider prose")
            # When
            failures = [
                await client.get(f"{CALLBACK_PATH}?state=s"),
                await client.get(f"{CALLBACK_PATH}?state=unknown&code=c"),
                await client.get(f"{CALLBACK_PATH}?{denial}"),
                await client.get(f"{CALLBACK_PATH}?{denial}"),
            ]
            recovered = await client.get(ROOT + "/status", params={"attempt_id": start.attempt_id})
    finally:
        app.dependency_overrides.clear()
    # Then
    assert [response.status_code for response in failures] == [400, 400, 400, 400]
    assert len({response.text for response in failures}) == 1
    for response in failures:
        _assert_isolated_page(response)
    assert "private provider prose" not in failures[2].text
    assert recovered.json()["status"] == "denied"


async def test_callback_storage_outage_is_503_before_provider_or_persistence(monkeypatch, oauth_store):
    # Given
    user = _user()
    app = _app(user)
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    persist = AsyncMock()
    monkeypatch.setattr(browser, "persist_subscription_tokens", persist)
    try:
        async with _client(app) as client:
            started = await client.post(ROOT + "/start")
            start = ChatGPTSubscriptionStartOut.model_validate(started.json())
            oauth_store.fail_on.add("getdel")
            # When
            response = await client.get(f"{CALLBACK_PATH}?{callback_query(start)}")
    finally:
        app.dependency_overrides.clear()
    # Then
    assert response.status_code == 503
    _assert_isolated_page(response)
    assert transport.calls == []
    persist.assert_not_awaited()


async def test_unconfigured_redirect_registers_no_callback_and_start_is_503(monkeypatch, oauth_store):
    # Given
    monkeypatch.setenv("CHATGPT_OAUTH_REDIRECT_URI", "")
    get_settings.cache_clear()
    user = _user()
    app = _app(user)
    try:
        # When
        async with _client(app) as client:
            started = await client.post(ROOT + "/start")
            callback = await client.get(f"{CALLBACK_PATH}?state=s&code=c")
    finally:
        app.dependency_overrides.clear()
    # Then
    assert build_callback_router().routes == []
    assert started.status_code == 503
    assert callback.status_code == 404
    assert oauth_store.rows == {}


async def test_callback_route_follows_the_configured_redirect_outside_the_schema(monkeypatch, oauth_store):
    # Given
    redirect_uri = "https://oauth.example.test:8443/team/%63hatgpt/callback"
    monkeypatch.setenv("CHATGPT_OAUTH_REDIRECT_URI", redirect_uri)
    get_settings.cache_clear()
    user = _user()
    app = _app(user)
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    monkeypatch.setattr(browser, "persist_subscription_tokens", AsyncMock(return_value=_model()))
    try:
        # When
        async with _client(app) as client:
            started = await client.post(ROOT + "/start")
            start = ChatGPTSubscriptionStartOut.model_validate(started.json())
            old_path = await client.get(f"{CALLBACK_PATH}?{callback_query(start)}")
            finished = await client.get(f"/team/%63hatgpt/callback?{callback_query(start)}")
    finally:
        app.dependency_overrides.clear()
    # Then
    handler = "app.api.chatgpt_oauth_callback.chatgpt_oauth_callback"
    callback_routes = [(route.path, route.methods) for route in effective_routes(app) if route.handler == handler]
    assert callback_routes == [("/team/chatgpt/callback", frozenset({"GET"}))]
    assert not any(path.startswith("/team/") for path in app.openapi()["paths"])
    assert parse_qs(urlsplit(start.authorization_url).query)["redirect_uri"] == [redirect_uri]
    assert old_path.status_code == 404
    assert finished.status_code == 200
    assert [form["redirect_uri"] for _, _, form in transport.calls] == [redirect_uri]


@pytest.mark.parametrize(
    ("role", "target", "allowed"),
    [("member", None, False), ("org_admin", "foreign", False), ("platform_admin", "foreign", True)],
)
async def test_start_tenant_rbac(oauth_store, role, target, allowed):
    # Given
    user = _user(role=role)
    tenant_id = str(uuid4()) if target else None
    # When / Then
    if allowed:
        result = await browser.start_chatgpt_subscription_handoff(user, tenant_id)
        assert result.attempt_id
    else:
        with pytest.raises(HTTPException) as exc:
            await browser.start_chatgpt_subscription_handoff(user, tenant_id)
        assert exc.value.status_code == 403
