"""LLM client factory cache and shared HTTP pool."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.llm import create_llm_client, create_llm_client_from_model
from app.services.llm.client_cache import reset_llm_client_cache
from app.services.llm.http_pool import acquire_httpx
from app.services.llm.providers.openai_compatible import OpenAICompatibleClient
from app.services.llm.providers.openai_responses import OpenAIResponsesClient


@pytest.fixture(autouse=True)
def _reset_clients():
    reset_llm_client_cache()
    yield
    reset_llm_client_cache()


def test_create_llm_client_reuses_same_wrapper_for_identical_config():
    first = create_llm_client("openai", "sk-live", "gpt-5.6", timeout=30)
    second = create_llm_client("openai", "sk-live", "gpt-5.6", timeout=30)
    other_key = create_llm_client("openai", "sk-other", "gpt-5.6", timeout=30)
    other_timeout = create_llm_client("openai", "sk-live", "gpt-5.6", timeout=60)
    assert first is second
    assert first is not other_key
    assert first is not other_timeout


def test_create_llm_client_from_model_reuses_row_and_skips_rebuild():
    row = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        auth_kind="api_key",
        api_key_encrypted="enc-key",
        oauth_account_id=None,
        provider="openai",
        model="gpt-5.6",
        base_url="https://api.openai.com/v1",
        request_timeout=45,
    )
    first = create_llm_client_from_model(row)
    second = create_llm_client_from_model(row)
    rotated = SimpleNamespace(**{**row.__dict__, "api_key_encrypted": "enc-key-rotated"})
    third = create_llm_client_from_model(rotated)
    assert first is second
    assert first is not third
    assert type(first) is OpenAICompatibleClient


def test_chatgpt_subscription_cache_includes_account_and_kind():
    row = SimpleNamespace(
        id="22222222-2222-2222-2222-222222222222",
        auth_kind="chatgpt_subscription",
        api_key_encrypted="enc-oa",
        oauth_account_id="acct-a",
        provider="openai",
        model="gpt-5.4",
        base_url="https://chatgpt.com/backend-api/codex",
        request_timeout=120,
    )
    first = create_llm_client_from_model(row)
    second = create_llm_client_from_model(row)
    other_acct = SimpleNamespace(**{**row.__dict__, "oauth_account_id": "acct-b"})
    third = create_llm_client_from_model(other_acct)
    assert type(first) is OpenAIResponsesClient
    assert first is second
    assert first is not third


@pytest.mark.asyncio
async def test_close_does_not_destroy_pooled_http_or_cached_wrapper():
    client = create_llm_client("openai", "sk-live", "gpt-5.6", timeout=12)
    assert type(client) is OpenAICompatibleClient
    http = await client._get_client()
    assert http is acquire_httpx(12)
    await client.close()
    assert client._client is None
    again = create_llm_client("openai", "sk-live", "gpt-5.6", timeout=12)
    assert again is client
    reused = await again._get_client()
    assert reused is http
    assert reused.is_closed is False
