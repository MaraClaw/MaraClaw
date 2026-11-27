"""Provider app attribution names ``settings.APP_NAME`` instead of OpenClaw."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import get_settings
from app.services import llm
from app.services.llm import http_pool
from app.services.llm.client_cache import reset_llm_client_cache
from app.services.llm.types import LLMMessage

APP_NAME = "Acme Workforce"
TITLE_HEADER = "x-openrouter-title"
COMPLETION = {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
MESSAGES = [LLMMessage(role="user", content="hi")]


@pytest.fixture(autouse=True)
def _isolated_clients(monkeypatch: pytest.MonkeyPatch):
    reset_llm_client_cache()
    monkeypatch.setattr(get_settings(), "APP_NAME", APP_NAME)
    yield
    reset_llm_client_cache()


def capture_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=COMPLETION)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_pool, "acquire_httpx", lambda timeout: client)
    return requests


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openrouter", None),
        ("custom", "https://openrouter.ai/api/v1/chat/completions"),
        ("custom", "https://eu.openrouter.ai/api/v1"),
    ],
)
async def test_openrouter_request_titles_with_app_name_not_openclaw(
    monkeypatch: pytest.MonkeyPatch, provider: str, base_url: str | None
) -> None:
    # Given
    requests = capture_requests(monkeypatch)
    client = llm.create_llm_client(provider, "sk-or-secret", "openrouter/auto", base_url=base_url)

    # When
    await client.complete(MESSAGES)

    # Then
    sent = requests[0].headers
    assert sent[TITLE_HEADER] == APP_NAME
    assert "openclaw" not in " ".join(sent.values()).lower()
    assert sent["authorization"] == "Bearer sk-or-secret"
    assert json.loads(requests[0].content)["model"] == "openrouter/auto"


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openai", None),
        ("deepseek", None),
        ("custom", "https://gateway.example/openrouter.ai/v1"),
        ("custom", "https://notopenrouter.ai/v1"),
        ("custom", "https://openrouter.ai.evil.example/v1"),
    ],
)
async def test_non_openrouter_endpoints_receive_no_title_header(
    monkeypatch: pytest.MonkeyPatch, provider: str, base_url: str | None
) -> None:
    # Given
    requests = capture_requests(monkeypatch)
    client = llm.create_llm_client(provider, "secret", "some-model", base_url=base_url)

    # When
    await client.complete(MESSAGES)

    # Then
    assert TITLE_HEADER not in requests[0].headers


async def test_title_follows_app_name_changes_without_rebuilding_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    requests = capture_requests(monkeypatch)
    client = llm.create_llm_client("openrouter", "sk-or-secret", "openrouter/auto")
    await client.complete(MESSAGES)

    # When
    monkeypatch.setattr(get_settings(), "APP_NAME", "Renamed")
    await client.complete(MESSAGES)

    # Then
    assert [r.headers[TITLE_HEADER] for r in requests] == [APP_NAME, "Renamed"]


@pytest.mark.parametrize(
    "app_name", ["", "   ", "Vi\u1ec7t Marathon", "\u6570\u5b57\u5458\u5de5", "Acme\r\nWorkforce", "Acme\tWorkforce"]
)
async def test_unsuitable_app_name_sends_no_title_header(monkeypatch: pytest.MonkeyPatch, app_name: str) -> None:
    # Given
    requests = capture_requests(monkeypatch)
    monkeypatch.setattr(get_settings(), "APP_NAME", app_name)
    client = llm.create_llm_client("openrouter", "sk-or-secret", "openrouter/auto")

    # When
    await client.complete(MESSAGES)

    # Then
    assert TITLE_HEADER not in requests[0].headers
