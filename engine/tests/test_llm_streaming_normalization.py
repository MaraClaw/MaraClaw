from collections.abc import Callable, Iterable
from types import TracebackType

import httpx
import pytest
from tests.llm_retry_fakes import (
    ANTHROPIC,
    CASE_PARAMS,
    FAILURES,
    STREAMING_CASE_PARAMS,
    Outcome,
    ProviderCase,
    ScriptedHttpClient,
    ScriptedResponse,
    call_provider,
    install_retry_policy,
)

from app.services.llm.base import LLMError
from app.services.llm.failover import FailoverErrorType, classify_error
from app.services.llm.providers import (
    AnthropicClient,
    GeminiClient,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
)
from app.services.llm.types import LLMMessage, LLMResponse


class FakeStreamResponse:
    def __init__(self, lines: Iterable[str], status_code: int = 200, body: bytes = b""):
        self.lines = tuple(lines)
        self.status_code = status_code
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def aiter_bytes(self):
        yield self.body


class FakeStreamingHttpClient:
    def __init__(self, response: FakeStreamResponse):
        self.response = response
        self.requests = []

    def stream(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, "kwargs": kwargs})
        return self.response


async def test_openai_compatible_stream_normalizes_partial_json_reasoning_usage_and_tool_deltas(monkeypatch):
    stream_lines = [
        'data: {"choices":[{"delta":{"content":"Hel',
        'data: lo "}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"think "}}]}',
        (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
            '"function":{"name":"lookup","arguments":"{\\"city\\": "}}]}}]}'
        ),
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"Paris\\"}"}}]}}]}',
        (
            'data: {"choices":[{"delta":{"content":"<think>hidden</think>world"},'
            '"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}'
        ),
        "data: [DONE]",
    ]
    http_client = FakeStreamingHttpClient(FakeStreamResponse(stream_lines))
    llm_client = OpenAICompatibleClient("secret", model="gpt-test")

    async def fake_get_client():
        return http_client

    chunks: list[str] = []
    thinking: list[str] = []
    tool_deltas = []

    async def on_chunk(text: str) -> None:
        chunks.append(text)

    async def on_thinking(text: str) -> None:
        thinking.append(text)

    async def on_tool_delta(delta) -> None:
        tool_deltas.append(delta)

    monkeypatch.setattr(llm_client, "_get_client", fake_get_client)

    response = await llm_client.stream(
        messages=[LLMMessage(role="user", content="hello")],
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "description": "", "parameters": {"type": "object", "properties": {}}},
            }
        ],
        temperature=0,
        max_tokens=32,
        on_chunk=on_chunk,
        on_thinking=on_thinking,
        on_tool_delta=on_tool_delta,
    )

    assert response.content == "Hello world"
    assert response.reasoning_content == "think "
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    assert response.tool_calls == [
        {
            "id": "call_1",
            "function": {"name": "lookup", "arguments": '{"city": "Paris"}'},
        }
    ]
    assert chunks == ["Hello ", "world"]
    assert thinking == ["think "]
    assert tool_deltas[-1] == {
        "id": "call_1",
        "index": 0,
        "name": "lookup",
        "arguments": '{"city": "Paris"}',
    }
    assert http_client.requests[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert http_client.requests[0]["kwargs"]["json"]["stream_options"] == {"include_usage": True}


async def test_anthropic_stream_normalizes_native_text_thinking_signature_tool_and_usage(monkeypatch):
    stream_lines = [
        "event: message_start",
        'data: {"message":{"model":"claude-stream","usage":{"input_tokens":10,"output_tokens":0}}}',
        "event: content_block_delta",
        'data: {"index":0,"delta":{"type":"thinking_delta","thinking":"plan"}}',
        "event: content_block_delta",
        'data: {"index":0,"delta":{"type":"signature_delta","signature":"sig-1"}}',
        "event: content_block_delta",
        'data: {"index":0,"delta":{"type":"text_delta","text":"Visible"}}',
        "event: content_block_start",
        'data: {"index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"search"}}',
        "event: content_block_delta",
        'data: {"index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\""}}',
        "event: content_block_delta",
        'data: {"index":1,"delta":{"type":"input_json_delta","partial_json":":\\"mara\\"}"}}',
        "event: message_delta",
        'data: {"delta":{"stop_reason":"tool_use"},"usage":{"input_tokens":10,"output_tokens":5}}',
        "event: message_stop",
        "data: {}",
    ]
    http_client = FakeStreamingHttpClient(FakeStreamResponse(stream_lines))
    llm_client = AnthropicClient("secret", model="claude-3-7")

    async def fake_get_client():
        return http_client

    chunks: list[str] = []
    thinking: list[str] = []
    tool_deltas = []

    async def on_chunk(text: str) -> None:
        chunks.append(text)

    async def on_thinking(text: str) -> None:
        thinking.append(text)

    async def on_tool_delta(delta) -> None:
        tool_deltas.append(delta)

    monkeypatch.setattr(llm_client, "_get_client", fake_get_client)

    response = await llm_client.stream(
        messages=[LLMMessage(role="user", content="hello")],
        tools=[
            {
                "type": "function",
                "function": {"name": "search", "description": "", "parameters": {"type": "object", "properties": {}}},
            }
        ],
        temperature=1,
        max_tokens=64,
        on_chunk=on_chunk,
        on_thinking=on_thinking,
        on_tool_delta=on_tool_delta,
    )

    assert response.content == "Visible"
    assert response.reasoning_content == "plan"
    assert response.reasoning_signature == "sig-1"
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"input_tokens": 10, "output_tokens": 5}
    assert response.model == "claude-stream"
    assert response.tool_calls == [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"q":"mara"}'},
        }
    ]
    assert chunks == ["Visible"]
    assert thinking == ["plan"]
    assert tool_deltas[-1] == {
        "id": "toolu_1",
        "index": 1,
        "name": "search",
        "arguments": '{"q":"mara"}',
    }
    assert http_client.requests[0]["url"] == "https://api.anthropic.com/v1/messages"


async def test_gemini_stream_normalizes_native_text_function_calls_usage_and_finish(monkeypatch):
    stream_lines = [
        (
            'data: {"candidates":[{"content":{"parts":[{"text":"Ge"},'
            '{"functionCall":{"name":"lookup","args":{"city":"Paris"},"id":"native-1"}}]},'
            '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":1,'
            '"candidatesTokenCount":2,"totalTokenCount":3}}'
        ),
        'data: {"candidates":[{"content":{"parts":[{"text":"mini"}]},"finishReason":"STOP"}]}',
    ]
    http_client = FakeStreamingHttpClient(FakeStreamResponse(stream_lines))
    llm_client = GeminiClient("secret", model="gemini-2.5-pro")

    async def fake_get_client():
        return http_client

    chunks: list[str] = []

    async def on_chunk(text: str) -> None:
        chunks.append(text)

    monkeypatch.setattr(llm_client, "_get_client", fake_get_client)

    response = await llm_client.stream(
        messages=[LLMMessage(role="user", content="hello")],
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "description": "", "parameters": {"type": "object", "properties": {}}},
            }
        ],
        temperature=0,
        max_tokens=64,
        on_chunk=on_chunk,
    )

    assert response.content == "Gemini"
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    assert response.model == "gemini-2.5-pro"
    assert response.tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"city": "Paris"}'},
            "_gemini_extra": {"id": "native-1"},
        }
    ]
    assert chunks == ["Ge", "mini"]
    assert http_client.requests[0]["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:streamGenerateContent"
    )
    assert http_client.requests[0]["kwargs"]["params"] == {"alt": "sse"}


async def test_openai_responses_stream_delegates_to_complete_and_forwards_callbacks(monkeypatch):
    llm_client = OpenAIResponsesClient("secret", model="gpt-responses")
    expected_response = LLMResponse(
        content="Final text",
        reasoning_content="Reasoning summary",
        tool_calls=[
            {
                "id": "call_resp",
                "type": "function",
                "function": {"name": "finish", "arguments": '{"content":"Final text"}'},
            }
        ],
        finish_reason="tool_calls",
        usage={"input_tokens": 4, "output_tokens": 5, "total_tokens": 9},
        model="gpt-responses",
    )
    complete_calls = []

    async def fake_complete(**kwargs):
        complete_calls.append(kwargs)
        return expected_response

    chunks: list[str] = []
    thinking: list[str] = []

    async def on_chunk(text: str) -> None:
        chunks.append(text)

    async def on_thinking(text: str) -> None:
        thinking.append(text)

    monkeypatch.setattr(llm_client, "complete", fake_complete)

    response = await llm_client.stream(
        messages=[LLMMessage(role="user", content="hello")],
        tools=[
            {
                "type": "function",
                "function": {"name": "finish", "description": "", "parameters": {"type": "object", "properties": {}}},
            }
        ],
        temperature=0,
        max_tokens=32,
        on_chunk=on_chunk,
        on_thinking=on_thinking,
    )

    assert response is expected_response
    assert chunks == ["Final text"]
    assert thinking == ["Reasoning summary"]
    assert complete_calls[0]["messages"] == [LLMMessage(role="user", content="hello")]
    assert complete_calls[0]["max_tokens"] == 32


async def test_openai_responses_stream_only_streams_events_and_complete_reads_the_same_stream(monkeypatch):
    # Given a backend that rejects non-streaming requests and returns an empty `output` in its final event.
    lines = [
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"Hel"}',
        'data: {"type":"response.output_text.delta","delta":"lo"}',
        'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"Hello"}]}}',
        'data: {"type":"response.completed","response":{"model":"gpt-codex","output":[],"usage":{"input_tokens":1,"output_tokens":2}}}',
    ]
    http_client = FakeStreamingHttpClient(FakeStreamResponse(lines))
    llm_client = OpenAIResponsesClient("secret", model="gpt-codex", stream_only=True)

    async def fake_get_client():
        return http_client

    monkeypatch.setattr(llm_client, "_get_client", fake_get_client)
    chunks: list[str] = []

    async def on_chunk(text: str) -> None:
        chunks.append(text)

    # When it is read through both entry points.
    streamed = await llm_client.stream(messages=[LLMMessage(role="user", content="hi")], on_chunk=on_chunk)
    http_client.response = FakeStreamResponse(lines)
    completed = await llm_client.complete(messages=[LLMMessage(role="user", content="hi")])

    # Then both requests asked for a stream and aggregated the same answer.
    assert [request["kwargs"]["json"]["stream"] for request in http_client.requests] == [True, True]
    assert streamed.content == completed.content == "Hello"
    assert chunks == ["Hel", "lo"]
    assert streamed.model == "gpt-codex"


async def test_openai_responses_stream_only_raises_on_error_event(monkeypatch):
    # Given a stream whose terminal event reports a failure.
    http_client = FakeStreamingHttpClient(
        FakeStreamResponse(
            ['data: {"type":"response.failed","response":{"status":"failed","error":{"message":"boom"}}}']
        )
    )
    llm_client = OpenAIResponsesClient("secret", model="gpt-codex", stream_only=True)

    async def fake_get_client():
        return http_client

    monkeypatch.setattr(llm_client, "_get_client", fake_get_client)

    # When / Then
    with pytest.raises(LLMError, match="boom"):
        await llm_client.complete(messages=[LLMMessage(role="user", content="hi")])


async def test_openai_compatible_stream_only_complete_sends_a_streaming_request(monkeypatch):
    # Given
    http_client = FakeStreamingHttpClient(
        FakeStreamResponse(
            [
                'data: {"choices":[{"delta":{"content":"ok"}}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
            ]
        )
    )
    llm_client = OpenAICompatibleClient("secret", model="grok-test", stream_only=True)

    async def fake_get_client():
        return http_client

    monkeypatch.setattr(llm_client, "_get_client", fake_get_client)

    # When
    response = await llm_client.complete(messages=[LLMMessage(role="user", content="hi")])

    # Then
    assert response.content == "ok"
    assert http_client.requests[0]["kwargs"]["json"]["stream"] is True


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    return install_retry_policy(monkeypatch)


@pytest.mark.parametrize("case", CASE_PARAMS)
@pytest.mark.parametrize("failure", FAILURES)
async def test_stream_failing_before_any_callback_is_retried_and_delivers_each_chunk_once(
    monkeypatch: pytest.MonkeyPatch, case: ProviderCase, failure: Callable[[], Outcome], sleeps: list[float]
) -> None:
    # Given a stream that fails while opening and a healthy one behind it.
    http = ScriptedHttpClient(failure(), case.ok())
    chunks: list[str] = []

    async def on_chunk(text: str) -> None:
        chunks.append(text)

    # When it is streamed.
    response = await call_provider(monkeypatch, case, "stream", http, on_chunk=on_chunk)
    # Then the second request answers and every chunk reached the callback exactly once.
    assert response.content == "Hello"
    assert chunks == list(case.chunks)
    assert len(http.requests) == 2
    assert sleeps == [0.75]


@pytest.mark.parametrize("case", STREAMING_CASE_PARAMS)
async def test_stream_disconnect_after_a_delivered_chunk_is_raised_not_retried(
    monkeypatch: pytest.MonkeyPatch, case: ProviderCase, sleeps: list[float]
) -> None:
    # Given a stream that delivers its first chunk and then drops, with a healthy one queued behind it.
    http = ScriptedHttpClient(case.partial(httpx.ReadError("dropped")), case.ok())
    chunks: list[str] = []

    async def on_chunk(text: str) -> None:
        chunks.append(text)

    # When it is streamed.
    with pytest.raises(LLMError, match=r"^Connection failed: dropped$") as raised:
        await call_provider(monkeypatch, case, "stream", http, on_chunk=on_chunk)
    # Then the chunk was delivered once, exactly one request was made and no wait happened.
    assert chunks == [case.chunks[0]]
    assert len(http.requests) == 1
    assert sleeps == []
    assert classify_error(raised.value) is FailoverErrorType.RETRYABLE


@pytest.mark.parametrize("case", STREAMING_CASE_PARAMS)
async def test_stream_disconnect_before_any_callback_restarts_from_a_clean_slate(
    monkeypatch: pytest.MonkeyPatch, case: ProviderCase, sleeps: list[float]
) -> None:
    # Given a stream that accumulates its first chunk, drops, and a caller that supplied no callbacks.
    http = ScriptedHttpClient(case.partial(httpx.ReadError("dropped")), case.ok())
    # When it is streamed.
    response = await call_provider(monkeypatch, case, "stream", http)
    # Then the retry starts over: the first chunk is not accumulated twice.
    assert response.content == "Hello"
    assert len(http.requests) == 2
    assert sleeps == [0.75]


@pytest.mark.parametrize(
    ("callback", "lines"),
    [
        (
            "on_thinking",
            ("event: content_block_delta", 'data: {"index":0,"delta":{"type":"thinking_delta","thinking":"plan"}}'),
        ),
        (
            "on_tool_delta",
            (
                "event: content_block_start",
                'data: {"index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"search"}}',
            ),
        ),
    ],
)
async def test_stream_thinking_or_tool_delta_delivery_also_blocks_the_retry(
    monkeypatch: pytest.MonkeyPatch, callback: str, lines: tuple[str, ...], sleeps: list[float]
) -> None:
    # Given a stream whose first event reaches the thinking or tool-delta callback before it drops.
    http = ScriptedHttpClient(ScriptedResponse(lines=lines, disconnect=httpx.ReadError("dropped")), ANTHROPIC.ok())
    seen: list[object] = []

    async def record(payload: object) -> None:
        seen.append(payload)

    # When it is streamed.
    with pytest.raises(LLMError, match=r"^Connection failed: dropped$"):
        await call_provider(monkeypatch, ANTHROPIC, "stream", http, **{callback: record})
    # Then the event was delivered once and nothing was replayed.
    assert len(seen) == 1
    assert len(http.requests) == 1
    assert sleeps == []
