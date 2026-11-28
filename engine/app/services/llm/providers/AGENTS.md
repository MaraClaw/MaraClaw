# app/services/llm/providers

Provider-specific LLM clients live here. They translate external protocols into the shared `LLMClient`, `LLMResponse`, and `LLMStreamChunk` contracts from the parent package.

## Provider Files

| Provider/protocol | Location | Notes |
|---|---|---|
| Anthropic | `anthropic.py` | Native payloads, prompt cache-control, thinking/signature streaming |
| Gemini | `gemini.py` | Native Gemini API plus OpenAI-compatible fallback for `/openai` base URLs |
| OpenAI-compatible | `openai_compatible.py` | Shared parser for OpenAI-style chat/stream endpoints and vendor flags |
| OpenAI Responses | `openai_responses.py` | Responses API protocol client |

## Conventions

- Add new protocol client construction in `app/services/llm/factory.py`; callers should not branch on provider names directly.
- Normalize provider output into parent-package DTOs. Do not leak provider-native response shapes above this layer.
- Streaming must preserve `on_chunk`, `on_tool_delta`, and `on_thinking` callbacks where the provider exposes those signals.
- Normalize finish reasons consistently: tool calls become tool-call completion, while stop/length/content-filter cases map to shared finish fields.
- Preserve usage normalization, including provider-specific cache/read/write token fields when available.
- Respect registry feature flags such as `supports_tool_choice`; OpenAI-compatible does not mean every endpoint supports every OpenAI option.
- Retries: wrap each request attempt in `retry_policy.call_with_retry` (`complete` posts through `raise_for_status`; `stream` is a thin wrapper holding a `StreamDelivery` around `_stream_once`, whose accumulators are local so a retry starts clean, and which raises `stream_http_error` for an error status). OpenAI Responses `stream` delegates to `complete`, so it inherits the retries, except with `stream_only` (subscription backends), where `stream` parses SSE in `_stream_once` under `StreamDelivery` and `complete` delegates to it. Pass every callback the stream can fire to `StreamDelivery`.
- Total deadline: every streamed line loop reads `deadline_lines(resp.aiter_lines(), request_timeout=self.timeout)` (`../stream_deadline.py`), so keepalives cannot hold a stream open past one wall-clock bound. Its `StreamDeadlineError` is an `LLMError` outside `RETRYABLE_TRANSPORT_ERRORS`: do not catch it, retry it or widen the retryable set for it, and give a new provider's stream the same wrapper.

## Provider-Specific Gotchas

- Anthropic prompt cache-control applies to selected system, last-user, and tool blocks. Keep cache annotations sparse and intentional.
- Anthropic thinking streams can include signatures; do not discard them unless the parent contract changes.
- Gemini native tool/function calls and usage chunks need normalization before they reach `caller.py`.
- OpenAI-compatible streaming parsers must tolerate partial JSON deltas, empty chunks, and vendor-specific reasoning fields.

## Avoid

- Keep provider parsing and the decision to retry in the owning provider modules; the shared policy is `../retry_policy.py`. No sleeps or retry loops in `caller.py`, `failover.py` or `http_pool.py`.
- Do not bypass the parent `finish` tool protocol or tool-call sanitization from provider code.
- HTTP is acquired from `app.services.llm.http_pool`. Wrapper `close()` detaches only; do not `aclose()` the pooled transport.
- Do not log API keys, request headers, raw credentials, or full prompt payloads in provider errors.
- Do not require live provider credentials in unit tests. Use fake HTTP/stream clients and deterministic response fragments.
