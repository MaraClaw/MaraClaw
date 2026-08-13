from collections.abc import Iterable
from types import TracebackType

from app.services.llm.client import (
    AnthropicClient,
    GeminiClient,
    LLMMessage,
    LLMResponse,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
)


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
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"function":{"name":"lookup","arguments":"{\\"city\\": "}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"Paris\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{"content":"<think>hidden</think>world"},'
        '"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}',
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
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
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
        tools=[{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}],
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
        'data: {"candidates":[{"content":{"parts":[{"text":"Ge"},'
        '{"functionCall":{"name":"lookup","args":{"city":"Paris"},"id":"native-1"}}]},'
        '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":1,'
        '"candidatesTokenCount":2,"totalTokenCount":3}}',
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
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
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
        tools=[{"type": "function", "function": {"name": "finish", "parameters": {"type": "object"}}}],
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
