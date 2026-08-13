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

- Add provider selection in `app/services/llm/registry.py` through `ProviderSpec`, aliases, protocol mappings, and token limits.
- Add new protocol client construction in `app/services/llm/factory.py`; callers should not branch on provider names directly.
- Normalize provider output into parent-package DTOs. Do not leak provider-native response shapes above this layer.
- Streaming must preserve `on_chunk`, `on_tool_delta`, and `on_thinking` callbacks where the provider exposes those signals.
- Normalize finish reasons consistently: tool calls become tool-call completion, while stop/length/content-filter cases map to shared finish fields.
- Preserve usage normalization, including provider-specific cache/read/write token fields when available.
- Respect registry feature flags such as `supports_tool_choice`; OpenAI-compatible does not mean every endpoint supports every OpenAI option.

## Provider-Specific Gotchas

- Anthropic prompt cache-control applies to selected system, last-user, and tool blocks. Keep cache annotations sparse and intentional.
- Anthropic thinking streams can include signatures; do not discard them unless the parent contract changes.
- Gemini native tool/function calls and usage chunks need normalization before they reach `caller.py`.
- OpenAI-compatible streaming parsers must tolerate partial JSON deltas, empty chunks, and vendor-specific reasoning fields.

## Avoid

- Do not add provider parsing or protocol-specific retries to `llm/client.py`; it is compatibility glue.
- Do not bypass the parent `finish` tool protocol or tool-call sanitization from provider code.
- Do not log API keys, request headers, raw credentials, or full prompt payloads in provider errors.
- Do not require live provider credentials in unit tests. Use fake HTTP/stream clients and deterministic response fragments.
