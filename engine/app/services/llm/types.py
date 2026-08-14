"""Shared LLM value types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

# ============================================================================
# Data Models
# ============================================================================


type LLMRole = Literal["system", "user", "assistant", "tool"]
type LLMUsage = dict[str, int]
type ToolPayloadValue = str | int | float | bool | dict[str, "ToolPayloadValue"] | list["ToolPayloadValue"] | None
type ToolPayload = dict[str, ToolPayloadValue]


class LLMContentPart(TypedDict, total=False):
    """OpenAI-compatible multimodal content part."""

    type: str
    text: str
    image_url: dict[str, str]


class LLMToolFunction(TypedDict, total=False):
    """Function details attached to a tool call."""

    name: str
    arguments: str | ToolPayload


class LLMToolCall(TypedDict, total=False):
    """Provider-neutral tool call payload."""

    id: str
    type: str
    function: LLMToolFunction
    index: int
    _gemini_extra: ToolPayload


class OpenAIMessage(TypedDict):
    """OpenAI-compatible message payload."""

    role: LLMRole
    content: NotRequired[str | list[LLMContentPart]]
    tool_calls: NotRequired[list[LLMToolCall]]
    tool_call_id: NotRequired[str]
    reasoning_content: NotRequired[str]


class AnthropicImageSource(TypedDict):
    """Base64-encoded Anthropic image source."""

    type: Literal["base64"]
    media_type: str
    data: str


class AnthropicContentBlock(TypedDict, total=False):
    """Anthropic content block emitted from a unified LLM message."""

    type: Literal["text", "image", "thinking", "tool_result", "tool_use"]
    text: str
    source: AnthropicImageSource
    thinking: str
    signature: str
    tool_use_id: str | None
    content: str | list[LLMContentPart] | list[AnthropicContentBlock]
    id: str
    name: str
    input: ToolPayload


class AnthropicMessage(TypedDict):
    """Anthropic-compatible message payload."""

    role: Literal["user", "assistant"]
    content: str | list[AnthropicContentBlock]


@dataclass
class LLMMessage:
    """Unified message format."""

    role: LLMRole
    content: str | list[LLMContentPart] | None = None
    tool_calls: list[LLMToolCall] | None = None
    tool_call_id: str | None = None
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    dynamic_content: str | None = None

    def to_openai_format(self) -> OpenAIMessage:
        """Convert to OpenAI format."""
        msg: OpenAIMessage = {"role": self.role}

        content = self.content
        if self.role == "system" and self.dynamic_content:
            content = f"{content}\n\n{self.dynamic_content}"

        if content is not None:
            msg["content"] = content
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.reasoning_content:
            msg["reasoning_content"] = self.reasoning_content
        return msg

    def to_anthropic_format(self) -> AnthropicMessage | None:
        """Convert to Anthropic format (returns None for system messages)."""
        if self.role == "system":
            return None

        role = self.role

        # Tool response (from user to assistant)
        if role == "tool":
            # Build tool_result content: support both string and vision array formats
            if isinstance(self.content, list):
                # Vision content array: extract text parts and image parts
                # Anthropic tool_result content supports [{type: "text", text: ...}, {type: "image", source: ...}]
                tool_content_blocks: list[AnthropicContentBlock] = []
                for part in self.content:
                    if part.get("type") == "text":
                        tool_content_blocks.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        # Convert OpenAI image_url format to Anthropic image source format
                        img_url = part.get("image_url", {}).get("url", "")
                        if img_url.startswith("data:image/"):
                            # Parse data URL: data:image/jpeg;base64,xxxxx
                            header, b64_data = img_url.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]  # e.g. image/jpeg
                            tool_content_blocks.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": b64_data,
                                    },
                                }
                            )
                result_content = tool_content_blocks if tool_content_blocks else (self.content or "")
            else:
                result_content = self.content or ""
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": self.tool_call_id,
                        "content": result_content,
                    }
                ],
            }

        content_blocks: list[AnthropicContentBlock] = []

        # Add reasoning/thinking content if present
        if self.role == "assistant" and self.reasoning_content:
            content_blocks.append(
                {
                    "type": "thinking",
                    "thinking": self.reasoning_content,
                    "signature": self.reasoning_signature or "synthetic_signature",
                }
            )

        if self.content:
            if isinstance(self.content, list):
                for part in self.content:
                    if part.get("type") == "text":
                        content_blocks.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        img_url = part.get("image_url", {}).get("url", "")
                        if img_url.startswith("data:image/"):
                            header, b64_data = img_url.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                            content_blocks.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": b64_data,
                                    },
                                }
                            )
            else:
                content_blocks.append({"type": "text", "text": self.content})

        # Tool requests (from assistant to user)
        if self.tool_calls:
            for tc in self.tool_calls:
                function_call = tc.get("function", LLMToolFunction())
                args = function_call.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                content_blocks.append(
                    {"type": "tool_use", "id": tc.get("id", ""), "name": function_call.get("name", ""), "input": args}
                )

        # Handle the structure
        if len(content_blocks) == 1 and content_blocks[0].get("type") == "text":
            content = content_blocks[0].get("text", "")
        else:
            content = content_blocks

        return {"role": role, "content": content}


@dataclass
class LLMResponse:
    """Unified response format."""

    content: str
    tool_calls: list[LLMToolCall] = field(default_factory=list[LLMToolCall])
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    finish_reason: str | None = None
    usage: LLMUsage | None = None
    model: str | None = None


@dataclass
class LLMStreamChunk:
    """Stream chunk format."""

    content: str = ""
    reasoning_content: str = ""
    tool_call: LLMToolCall | None = None
    finish_reason: str | None = None
    is_finished: bool = False
    usage: LLMUsage | None = None
