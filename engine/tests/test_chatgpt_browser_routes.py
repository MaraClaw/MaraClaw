from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from tests.chatgpt_subscription_fakes import callback, oauth_store

__all__ = ["oauth_store"]
from tests.test_chatgpt_subscription import FakeChatGPTTransport, _user

from app.api import enterprise
from app.services import chatgpt_oauth as oauth, chatgpt_subscription_handoff as browser
from app.services.chatgpt_subscription_types import ChatGPTSubscriptionStartOut


async def test_http_start_complete_and_read_only_recovery_contract(oauth_store):
    # Given
    app = FastAPI()
    app.include_router(enterprise.router, prefix="/api")
    user = _user()
    app.dependency_overrides[enterprise.get_current_admin] = lambda: user
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    root = "/api/enterprise/llm-models/chatgpt-subscription"
    # When
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        started = await client.post(root + "/start")
        assert started.status_code == 200
        start = ChatGPTSubscriptionStartOut.model_validate(started.json())
        body = callback(start, error="access_denied").model_dump()
        invalid = await client.post(root + "/complete", json={**body, "code_verifier": "not-accepted"})
        pending = await client.get(root + "/status", params={"attempt_id": start.attempt_id})
        complete = await client.post(root + "/complete", json=body)
        recovered = await client.get(root + "/status", params={"attempt_id": start.attempt_id})
    # Then
    assert set(started.json()) == {"attempt_id", "authorization_url", "redirect_uri", "expires_in"}
    assert invalid.status_code == 422
    assert pending.json()["status"] == "pending"
    assert complete.status_code == recovered.status_code == 200
    assert complete.json() == recovered.json()
    assert complete.json()["status"] == "denied"
    assert set(complete.json()) == {"status", "attempt_id", "model_id", "detail", "expires_in"}
    assert transport.calls == []


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
