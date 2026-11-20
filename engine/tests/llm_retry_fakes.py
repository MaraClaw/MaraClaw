"""Scripted HTTP fakes and per-provider fixtures for the LLM retry tests.

Nothing here sleeps or touches the network: ``install_retry_policy`` swaps the shared policy's sleep for a
recorder and ``ScriptedHttpClient`` plays back the outcomes a test queues.
"""

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace, TracebackType
from typing import Any, Literal

import httpx
import pytest

from app.services.llm import retry_policy
from app.services.llm.base import ChunkCallback, LLMClient, ThinkingCallback, ToolCallback
from app.services.llm.providers import AnthropicClient, GeminiClient, OpenAICompatibleClient, OpenAIResponsesClient
from app.services.llm.retry_policy import RetryPolicy
from app.services.llm.types import LLMMessage, LLMResponse

API_KEY = "sk-live-SECRET-4f9c"
PROMPT = "private prompt body 7d21"
FIXED_NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)

type Mode = Literal["complete", "stream"]


class ScriptedResponse:
    """Stand-in for an ``httpx.Response`` that serves both ``post`` and ``stream`` callers."""

    def __init__(
        self,
        status_code: int = 200,
        *,
        headers: dict[str, str] | None = None,
        body: str = "",
        json_body: dict[str, object] | None = None,
        lines: Iterable[str] = (),
        disconnect: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = body
        self.json_body = json_body or {}
        self.lines = tuple(lines)
        self.disconnect = disconnect
        self.closed = False

    def json(self) -> dict[str, object]:
        return self.json_body

    async def aclose(self) -> None:
        self.closed = True

    async def aiter_bytes(self):
        yield self.text.encode()

    async def aiter_lines(self):
        for line in self.lines:
            yield line
        if self.disconnect is not None:
            raise self.disconnect


type Outcome = ScriptedResponse | Exception


class _StreamContext:
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome

    async def __aenter__(self) -> ScriptedResponse:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if isinstance(self.outcome, ScriptedResponse):
            self.outcome.closed = True
        return False


class ScriptedHttpClient:
    """Plays back queued outcomes in order; a request with nothing queued fails the test."""

    def __init__(self, *script: Outcome) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def _next(self, method: str, url: str, kwargs: dict[str, object]) -> Outcome:
        self.requests.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.script:
            raise AssertionError("unexpected extra request")
        return self.script.pop(0)

    async def post(self, url: str, **kwargs: object) -> ScriptedResponse:
        outcome = self._next("POST", url, kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stream(self, method: str, url: str, **kwargs: object) -> _StreamContext:
        return _StreamContext(self._next(method, url, kwargs))


@dataclass(frozen=True)
class ProviderCase:
    """One provider client plus the wire answers that make it produce ``Hello``."""

    id: str
    make: Callable[[], LLMClient]
    complete_json: dict[str, object]
    stream_lines: tuple[str, ...]
    chunks: tuple[str, ...]
    first_chunk_lines: int
    streams_natively: bool = True

    def ok(self) -> ScriptedResponse:
        return ScriptedResponse(json_body=self.complete_json, lines=self.stream_lines)

    def partial(self, error: Exception) -> ScriptedResponse:
        """A stream that delivers its first chunk and then drops."""
        return ScriptedResponse(lines=self.stream_lines[: self.first_chunk_lines], disconnect=error)


OPENAI_COMPATIBLE = ProviderCase(
    id="openai-compatible",
    make=lambda: OpenAICompatibleClient(API_KEY, model="gpt-test"),
    complete_json={"choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}]},
    stream_lines=(
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ),
    chunks=("Hel", "lo"),
    first_chunk_lines=1,
)
ANTHROPIC = ProviderCase(
    id="anthropic",
    make=lambda: AnthropicClient(API_KEY, model="claude-test"),
    complete_json={"content": [{"type": "text", "text": "Hello"}], "stop_reason": "end_turn"},
    stream_lines=(
        "event: message_start",
        'data: {"message":{"model":"claude-test"}}',
        "event: content_block_delta",
        'data: {"index":0,"delta":{"type":"text_delta","text":"Hel"}}',
        "event: content_block_delta",
        'data: {"index":0,"delta":{"type":"text_delta","text":"lo"}}',
        "event: message_stop",
        "data: {}",
    ),
    chunks=("Hel", "lo"),
    first_chunk_lines=4,
)
GEMINI = ProviderCase(
    id="gemini",
    make=lambda: GeminiClient(API_KEY, model="gemini-test"),
    complete_json={"candidates": [{"content": {"parts": [{"text": "Hello"}]}, "finishReason": "STOP"}]},
    stream_lines=(
        'data: {"candidates":[{"content":{"parts":[{"text":"Hel"}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":"lo"}]},"finishReason":"STOP"}]}',
    ),
    chunks=("Hel", "lo"),
    first_chunk_lines=1,
)
OPENAI_RESPONSES = ProviderCase(
    id="openai-responses",
    make=lambda: OpenAIResponsesClient(API_KEY, model="gpt-responses"),
    complete_json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}]},
    stream_lines=(),
    chunks=("Hello",),
    first_chunk_lines=0,
    streams_natively=False,
)

CASES = (OPENAI_COMPATIBLE, ANTHROPIC, GEMINI, OPENAI_RESPONSES)
CASE_PARAMS = [pytest.param(case, id=case.id) for case in CASES]
STREAMING_CASE_PARAMS = [pytest.param(case, id=case.id) for case in CASES if case.streams_natively]
CALLS = [pytest.param(case, mode, id=f"{case.id}-{mode}") for case in CASES for mode in ("complete", "stream")]

FAILURES: list[object] = [
    pytest.param(lambda: ScriptedResponse(429, body="rate limit hit"), id="429"),
    pytest.param(lambda: ScriptedResponse(503, body="upstream busy"), id="503"),
    pytest.param(lambda: httpx.ConnectError("boom"), id="connect-error"),
]


def install_retry_policy(
    monkeypatch: pytest.MonkeyPatch, sleep: Callable[[float], Awaitable[None]] | None = None
) -> list[float]:
    """Replace the shared policy with a deterministic one and return the list its waits are recorded in."""
    sleeps: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if sleep is not None:
            await sleep(seconds)

    policy = RetryPolicy(sleep=recording_sleep, jitter=lambda: 0.5, now=lambda: FIXED_NOW)
    monkeypatch.setattr(retry_policy, "RETRY_POLICY", policy)
    return sleeps


def capture_retry_logs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    logged: list[str] = []
    recorder = SimpleNamespace(warning=lambda message, *args: logged.append(str(message).format(*args)))
    monkeypatch.setattr(retry_policy, "logger", recorder)
    return logged


async def call_provider(
    monkeypatch: pytest.MonkeyPatch,
    case: ProviderCase,
    mode: Mode,
    http: ScriptedHttpClient,
    *,
    on_chunk: ChunkCallback | None = None,
    on_tool_delta: ToolCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
) -> LLMResponse:
    """Call ``complete`` or ``stream`` on a fresh client whose pooled HTTP client is ``http``."""
    client = case.make()

    async def get_client() -> ScriptedHttpClient:
        return http

    monkeypatch.setattr(client, "_get_client", get_client)
    messages = [LLMMessage(role="user", content=PROMPT)]
    if mode == "complete":
        return await client.complete(messages=messages)
    return await client.stream(
        messages=messages, on_chunk=on_chunk, on_tool_delta=on_tool_delta, on_thinking=on_thinking
    )
