from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.chatgpt_subscription_fakes import _ACCESS, _NOW, _model, _user

from app.api import enterprise as enterprise_api
from app.config import get_settings
from app.core.security import encrypt_data


@pytest.mark.asyncio
async def test_probe_uses_codex_responses_client(monkeypatch):
    existing = _model(api_key_encrypted=encrypt_data(_ACCESS, get_settings().SECRET_KEY), oauth_account_id="acct-live")
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=existing))
    monkeypatch.setattr(
        "app.services.chatgpt_subscription.ensure_fresh_access_token",
        AsyncMock(return_value=existing),
    )
    captured: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                "output_text": "ok",
            }

    class FakeHttpx:
        is_closed = False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("app.services.llm.http_pool.acquire_httpx", lambda _timeout: FakeHttpx())
    monkeypatch.setattr(
        enterprise_api,
        "create_llm_client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not use chat-completions")),
    )

    result = await enterprise_api.probe_llm_model(
        enterprise_api.LLMTestRequest(
            provider="openai",
            model="gpt-5.4",
            model_id=str(existing.id),
        ),
        current_user=_user(),
    )
    assert result["success"] is True
    assert result.get("reply") == "ok"
    assert str(captured["url"]).endswith("/responses")
    assert "codex" in str(captured["url"])
    assert captured["json"]["store"] is False
    assert "temperature" not in captured["json"]
    assert "max_output_tokens" not in captured["json"]
    assert captured["headers"]["chatgpt-account-id"] == "acct-live"


def test_create_llm_client_from_model_uses_codex_responses():
    from app.services.llm.providers.openai_responses import OpenAIResponsesClient
    from app.services.llm.utils import create_llm_client_from_model

    row = _model(api_key_encrypted=encrypt_data(_ACCESS, get_settings().SECRET_KEY), oauth_account_id="acct-stored")
    client = create_llm_client_from_model(row, timeout=9)
    assert type(client) is OpenAIResponsesClient
    assert client.stateless is True
    assert client.base_url == "https://chatgpt.com/backend-api/codex"
    headers = client._get_headers()
    assert headers["chatgpt-account-id"] == "acct-stored"
    assert headers["originator"] == "codex_cli_rs"
    payload = client._build_payload([], None, None, 16)
    assert payload["store"] is False
    assert "max_output_tokens" not in payload
    assert "temperature" not in payload
    effort_payload = client._build_payload([], None, None, 16, llm_provider="openai", reasoning_effort="high")
    assert effort_payload.get("reasoning") == {"effort": "high"}
    assert "reasoning_effort" not in effort_payload

    parsed = client._parse_response_data(
        {
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": "enc-reason",
                    "summary": [{"type": "summary_text", "text": "think"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "search",
                    "arguments": "{}",
                },
            ]
        }
    )
    from app.services.llm.types import LLMMessage

    follow = client._build_payload(
        [
            LLMMessage(
                role="assistant",
                tool_calls=parsed.tool_calls,
                provider_items=parsed.provider_items,
            ),
            LLMMessage(role="tool", tool_call_id="call_1", content="ok"),
        ],
        None,
        None,
        16,
    )
    assert follow["input"][0]["type"] == "reasoning"
    assert follow["input"][0]["encrypted_content"] == "enc-reason"
    assert follow["input"][1]["type"] == "function_call"
    assert follow["input"][2]["type"] == "function_call_output"


@pytest.mark.asyncio
async def test_create_fresh_refreshes_expired_chatgpt_row(monkeypatch):
    expired = _model(token_expires_at=_NOW - timedelta(minutes=1))
    refreshed = _model(api_key_encrypted=encrypt_data("new", get_settings().SECRET_KEY), oauth_account_id="acct-live")
    ensure = AsyncMock(return_value=refreshed)
    monkeypatch.setattr("app.services.chatgpt_subscription.ensure_fresh_access_token", ensure)
    from app.services.llm.utils import create_fresh_llm_client_from_model

    model, client = await create_fresh_llm_client_from_model(expired, timeout=9)
    ensure.assert_awaited_once()
    assert model is refreshed
    assert type(client).__name__ == "OpenAIResponsesClient"
