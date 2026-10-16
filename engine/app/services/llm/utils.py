"""Shared LLM provider configuration and utilities.

Centralizes provider URLs and provider-specific API parameters
so they don't need to be duplicated across websocket.py, scheduler.py,
task_executor.py, agent_tools.py, and feishu.py.

This module also exports shared LLM types, clients, and factory helpers
from their owning modules for convenient access.
"""

from collections.abc import Iterable
from typing import Protocol, overload

from app.config import get_settings
from app.core.json_types import json_as_str, json_as_str_or, json_loads_object
from app.core.logging import logger
from app.core.security import decrypt_data
from app.records.chat import ChatMessageRecord
from app.records.llm import LLMModelRecord

from .base import LLMClient, LLMError
from .factory import chat_complete, chat_stream, create_llm_client
from .providers import AnthropicClient, GeminiClient, OpenAICompatibleClient, OpenAIResponsesClient
from .registry import (
    PROVIDER_ALIASES,
    PROVIDER_REGISTRY,
    PROVIDER_URLS,
    TOOL_CHOICE_PROVIDERS,
    ProviderSpec,
    get_max_tokens,
    get_provider_base_url,
    get_provider_manifest,
    get_provider_spec,
    normalize_provider,
)
from .types import LLMMessage, LLMResponse, LLMStreamChunk, OpenAIMessage


class _EncryptedModel(Protocol):
    @property
    def api_key_encrypted(self) -> str | None: ...


class _ClientModel(_EncryptedModel, Protocol):
    @property
    def request_timeout(self) -> int | None: ...


def get_model_api_key(model: _EncryptedModel) -> str:
    """Decrypt a stored model API key; invalid ciphertext is rejected."""
    raw = model.api_key_encrypted or ""
    if not raw:
        return ""
    settings = get_settings()
    return decrypt_data(raw, settings.SECRET_KEY)


@overload
async def ensure_fresh_pool_model(model: LLMModelRecord) -> LLMModelRecord: ...


@overload
async def ensure_fresh_pool_model[T: _EncryptedModel](model: T) -> T | LLMModelRecord: ...


async def ensure_fresh_pool_model(model: _EncryptedModel) -> _EncryptedModel:
    """Refresh a Grok or ChatGPT subscription row when the access token is stale."""
    kind = getattr(model, "auth_kind", "") or ""
    if kind == "chatgpt_subscription" and isinstance(model, LLMModelRecord):
        from app.services.chatgpt_subscription import ensure_fresh_access_token

        return await ensure_fresh_access_token(model)
    if kind == "grok_subscription" and isinstance(model, LLMModelRecord):
        from app.services.grok_subscription import ensure_fresh_access_token

        return await ensure_fresh_access_token(model)
    return model


@overload
async def create_fresh_llm_client_from_model(
    model: LLMModelRecord,
    *,
    timeout: float | None = None,  # noqa: ASYNC109
) -> tuple[LLMModelRecord, LLMClient]: ...


@overload
async def create_fresh_llm_client_from_model[T: _ClientModel](
    model: T,
    *,
    timeout: float | None = None,  # noqa: ASYNC109
) -> tuple[T | LLMModelRecord, LLMClient]: ...


async def create_fresh_llm_client_from_model(
    model: _ClientModel,
    *,
    timeout: float | None = None,  # noqa: ASYNC109
) -> tuple[_ClientModel, LLMClient]:
    """Refresh subscription tokens, then build (or reuse) a client."""
    fresh = await ensure_fresh_pool_model(model)
    return fresh, create_llm_client_from_model(fresh, timeout=timeout)


def create_llm_client_from_model(model: _ClientModel, *, timeout: float | None = None) -> LLMClient:
    """Build a client from a pool row. ChatGPT subscription uses Codex Responses."""
    from app.services.llm.client_cache import cache_get, cache_put, fingerprint_secret

    resolved_timeout = timeout
    if resolved_timeout is None:
        raw_timeout = model.request_timeout
        resolved_timeout = float(raw_timeout) if raw_timeout not in (None, 0) else 120.0
    enc = getattr(model, "api_key_encrypted", None) or ""
    cache_key = (
        "from_model",
        str(getattr(model, "id", "") or ""),
        getattr(model, "auth_kind", "") or "api_key",
        fingerprint_secret(enc),
        getattr(model, "oauth_account_id", None) or "",
        getattr(model, "provider", "") or "",
        getattr(model, "model", None) or "",
        getattr(model, "base_url", None) or "",
        resolved_timeout,
    )
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    if getattr(model, "auth_kind", "") == "chatgpt_subscription":
        from app.services.chatgpt_oauth import (
            CHATGPT_CODEX_BASE_URL,
            account_id_from_jwt,
            codex_request_headers,
        )

        access = get_model_api_key(model)
        account_id = (getattr(model, "oauth_account_id", None) or "").strip() or account_id_from_jwt(access)
        built = OpenAIResponsesClient(
            api_key=access,
            base_url=getattr(model, "base_url", None) or CHATGPT_CODEX_BASE_URL,
            model=getattr(model, "model", None),
            timeout=resolved_timeout,
            extra_headers=codex_request_headers(account_id),
            stateless=True,
        )
        return cache_put(cache_key, built)
    built = create_llm_client(
        provider=getattr(model, "provider", "") or "",
        api_key=get_model_api_key(model),
        model=getattr(model, "model", None) or "",
        base_url=getattr(model, "base_url", None),
        timeout=resolved_timeout,
    )
    return cache_put(cache_key, built)


def convert_chat_messages_to_llm_format(messages: Iterable[ChatMessageRecord]) -> list[OpenAIMessage]:
    """Convert ChatMessage DB records to LLM-compatible message dicts.

    Properly handles ``tool_call`` role records by splitting them into an
    assistant message (with ``tool_calls`` array) followed by a tool result
    message - the format required by OpenAI / Anthropic / Gemini APIs.

    Without this conversion, ``tool_call`` records would be passed with
    ``role="tool_call"`` (an invalid role), causing LLM API errors or
    silently lost context.

    Args:
        messages: Iterable of ChatMessage ORM objects (with ``role``,
                  ``content``, ``id``, and optional ``thinking`` attributes).

    Returns:
        List of dicts suitable for passing to ``call_llm()`` or
        ``call_llm_with_failover()``.
    """
    import json as _json

    result: list[OpenAIMessage] = []
    for msg in messages:
        if msg.role == "tool_call":
            try:
                tc_data = json_loads_object(msg.content)
                tc_name = json_as_str_or(tc_data.get("name"), "unknown")
                tc_args = tc_data.get("args", {})
                tc_result = tc_data.get("result", "")
                tc_id = f"call_{msg.id}"  # synthetic tool_call_id

                # Assistant message with tool_calls array
                asst_msg: OpenAIMessage = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {
                                "name": tc_name,
                                "arguments": _json.dumps(tc_args, ensure_ascii=False),
                            },
                        }
                    ],
                }
                reasoning_content = json_as_str(tc_data.get("reasoning_content"))
                if reasoning_content not in (None, ""):
                    asst_msg["reasoning_content"] = reasoning_content
                result.append(asst_msg)

                # Tool result message
                try:
                    from app.services.vision_inject import sanitize_history_tool_result

                    sanitized_result = sanitize_history_tool_result(str(tc_result))
                except ImportError:
                    sanitized_result = str(tc_result)
                result.append({"role": "tool", "tool_call_id": tc_id, "content": sanitized_result[:500]})
            except Exception as exc:
                logger.debug("Skipping malformed tool-call history record {}: {}", msg.id, type(exc).__name__)
                continue
        else:
            if msg.role == "assistant":
                entry: OpenAIMessage = {"role": "assistant", "content": msg.content}
            elif msg.role == "system":
                entry = OpenAIMessage(role="system", content=msg.content)
            elif msg.role == "tool":
                entry = OpenAIMessage(role="tool", content=msg.content)
            else:
                entry = OpenAIMessage(role="user", content=msg.content)
            if hasattr(msg, "thinking") and msg.thinking not in (None, ""):
                entry["reasoning_content"] = msg.thinking
            result.append(entry)

    return result


def truncate_messages_with_pair_integrity(messages: list[OpenAIMessage], ctx_size: int) -> list[OpenAIMessage]:
    """Truncate message list to ctx_size while preserving assistant+tool pair integrity.

    When context window truncation breaks an assistant(tool_calls) + tool_result
    group, the resulting orphaned messages cause "No tool call found for function
    call output" errors from the LLM API. This function ensures that:

    1. No tool_result message exists without its preceding assistant(tool_calls)
    2. No assistant(tool_calls) message exists without all its tool_result messages
    """
    truncated = messages[-ctx_size:]
    if not truncated:
        return truncated

    # Pass 1: Remove leading tool messages (they have no matching assistant before them)
    while truncated and truncated[0].get("role") == "tool":
        _ = truncated.pop(0)

    if not truncated:
        return truncated

    # Pass 2: Scan for broken pairs within the truncated list.
    assistant_call_ids: set[str] = set()
    tool_call_ids: set[str] = set()

    for msg in truncated:
        tool_calls = msg.get("tool_calls")
        if msg.get("role") == "assistant" and tool_calls is not None and tool_calls:
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                if tc_id:
                    assistant_call_ids.add(tc_id)
        elif msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id:
                tool_call_ids.add(tc_id)

    orphaned_tools = tool_call_ids - assistant_call_ids
    orphaned_assistant_calls = assistant_call_ids - tool_call_ids

    if not orphaned_tools and not orphaned_assistant_calls:
        return truncated

    # Remove orphaned tool messages and assistant tool_calls entries
    sanitized: list[OpenAIMessage] = []
    for msg in truncated:
        tool_calls = msg.get("tool_calls")
        if msg.get("role") == "tool":
            if msg.get("tool_call_id", "") in orphaned_tools:
                continue  # Remove orphaned tool result
        elif msg.get("role") == "assistant" and tool_calls is not None and tool_calls:
            filtered_tcs = [tc for tc in tool_calls if tc.get("id", "") not in orphaned_assistant_calls]
            if filtered_tcs:
                new_msg: OpenAIMessage = {
                    "role": "assistant",
                    "tool_calls": filtered_tcs,
                }
                content = msg.get("content")
                if isinstance(content, (str, list)):
                    new_msg["content"] = content
                reasoning_content = msg.get("reasoning_content")
                if isinstance(reasoning_content, str):
                    new_msg["reasoning_content"] = reasoning_content
                sanitized.append(new_msg)
            elif (content := msg.get("content")) is not None and content:
                content = msg.get("content")
                if not isinstance(content, (str, list)):
                    continue
                new_msg = OpenAIMessage(role="assistant", content=content)
                reasoning_content = msg.get("reasoning_content")
                if isinstance(reasoning_content, str):
                    new_msg["reasoning_content"] = reasoning_content
                sanitized.append(new_msg)
            # else: drop the entire assistant message (no content, no valid tool_calls)
        else:
            sanitized.append(msg)

    return sanitized


__all__ = [  # noqa: RUF022 - ordering is a tested public compatibility contract
    "get_provider_base_url",
    "get_max_tokens",
    "get_model_api_key",
    "convert_chat_messages_to_llm_format",
    "truncate_messages_with_pair_integrity",
    "LLMClient",
    "OpenAICompatibleClient",
    "OpenAIResponsesClient",
    "GeminiClient",
    "AnthropicClient",
    "LLMMessage",
    "LLMResponse",
    "LLMStreamChunk",
    "LLMError",
    "create_llm_client",
    "create_llm_client_from_model",
    "create_fresh_llm_client_from_model",
    "ensure_fresh_pool_model",
    "chat_complete",
    "chat_stream",
    "ProviderSpec",
    "PROVIDER_ALIASES",
    "PROVIDER_REGISTRY",
    "PROVIDER_URLS",
    "TOOL_CHOICE_PROVIDERS",
    "normalize_provider",
    "get_provider_spec",
    "get_provider_manifest",
]
