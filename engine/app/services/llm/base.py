"""Base LLM client interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import Any, Literal, TypedDict

from app.core.tool_types import ToolDefinition
from app.services.llm.types import LLMMessage, LLMResponse, ToolPayload

# ============================================================================
# Type Definitions
# ============================================================================

ChunkCallback = Callable[[str], Coroutine[Any, Any, None]]
ThinkingCallback = Callable[[str], Coroutine[Any, Any, None]]


class ToolCallbackData(TypedDict, total=False):
    """Incremental or completed tool-call callback payload."""

    name: str
    id: str
    call_id: str
    args: ToolPayload
    status: Literal["running", "done"]
    result: str
    reasoning_content: str
    index: int
    arguments: str


ToolCallback = Callable[[ToolCallbackData], Coroutine[Any, Any, None]]


class LLMError(Exception):
    """Base exception for LLM client errors."""


# ============================================================================
# Base Client Interface
# ============================================================================


class LLMClient(ABC):
    """Abstract base class for LLM clients."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ):
        self.api_key: str = api_key
        self.base_url: str | None = base_url
        self.model: str | None = model
        self.timeout: float = timeout

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a completion request and return the full response."""

    @abstractmethod
    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_chunk: ChunkCallback | None = None,
        on_tool_delta: ToolCallback | None = None,
        on_thinking: ThinkingCallback | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a streaming request and return the aggregated response."""

    @abstractmethod
    def _get_headers(self) -> dict[str, str]:
        """Get request headers."""

    async def close(self) -> None:
        return None
