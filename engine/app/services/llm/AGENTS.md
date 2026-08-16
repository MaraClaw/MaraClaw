# app/services/llm

This package owns provider protocol handling and agent-facing LLM orchestration.

## Layers

- `base.py` defines the abstract `LLMClient`, callback types, and `LLMError`.
- `types.py` defines shared message/response/stream/tool-call shapes and provider-format conversion helpers.
- `registry.py` owns `ProviderSpec`, aliases, provider manifests, protocol/client mappings, base URLs, and token-limit helpers.
- `factory.py` owns `create_llm_client(...)`, `chat_complete(...)`, and `chat_stream(...)`.
- `providers/` contains provider/protocol clients. Read `providers/AGENTS.md` before changing protocol parsing or streaming normalization.
- `client.py` is now a compatibility/public-contract surface around the split modules; do not move large new provider logic back into it.
- `caller.py` is the agent orchestration layer. Agent/chat/trigger/websocket flows should enter through `call_agent_llm`, `call_agent_llm_with_tools`, `call_llm`, or `call_llm_with_failover`.
- `router.py` is the complexity preflight. Conversational turns call `select_turn_model` once, then pass the **selected** model into `call_llm_with_failover`. Fallback is a failure lane, never a cheap-model substitute. Do not grow this logic inside `caller.py`.
- `turn.py` is the preloaded `TurnContext` / `ModelBundle` (agent/models/user) so callers skip redundant DAO loads. Do not hold a DB connection across `call_llm*`.
- `utils.py` handles encrypted API-key compatibility, chat-history conversion, and truncation that preserves assistant/tool-result pairs.
- `reasoning.py` maps canonical `none|low|medium|high|xhigh` onto provider-native request fields. Do not encode those translations in individual provider clients.
- `vision_content.py` builds vision/screenshot parts when the selected model supports vision.
- `finish.py` defines the required `finish(content=...)` control tool.
- `failover.py` classifies retryable provider failures.

## Provider Rules

- Runtime provider config comes from `llm_model_dao` / `LLMModelRecord` rows, not direct env vars: provider, model, encrypted API key, base URL, timeout, temperature, vision support, max output tokens.
- Unknown providers currently fall back to OpenAI-compatible behavior; do not remove that compatibility without migrating stored model rows.
- Preserve streaming normalization for tool-call ids, partial JSON deltas, usage chunks, reasoning/thinking fields, and provider-native message shapes.
- New providers should be added through `ProviderSpec`/registry plus a provider client class or explicit OpenAI-compatible mapping.
- Grok (xAI) is registered as `grok` (`xai` / `x-ai` / `x_ai` aliases) on the OpenAI-compatible protocol at `https://api.x.ai/v1`.
- `ProviderSpec.default_model` is the current flagship API id for the admin Models form (e.g. `grok-4.6`, `gpt-5.6`). Local runtimes may leave it unset.

## Tool Loop

- Final assistant text is expected through the `finish` tool. Do not bypass finish-protocol enforcement casually.
- Tool calls are sanitized before appending back into model context.
- Tool execution goes through `app.services.agent_tools.execute_tool(...)`.
- Vision/screenshot injection is conditional on the selected model supporting vision.

## Tests

- Existing focused coverage includes `tests/test_finish_protocol.py`, `tests/test_llm_client_contract.py`, and `tests/test_llm_streaming_normalization.py`.
- Add fake HTTP/stream parser tests for provider client changes.
- Add pair-integrity tests when changing `convert_chat_messages_to_llm_format` or `truncate_messages_with_pair_integrity`.
