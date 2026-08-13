"""Runtime tool schema TypedDicts.

Tool catalog typing for request/worker code
without importing the ORM layer.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from app.core.json_types import JsonObject


class ToolParameterSchema(TypedDict):
    """OpenAI function-calling parameter schema persisted for a tool."""

    type: str
    properties: dict[str, JsonObject]
    required: NotRequired[list[str]]


class ToolFunctionDefinition(TypedDict):
    """OpenAI function details assembled for an LLM tool catalog."""

    name: str
    description: str
    parameters: ToolParameterSchema


class ToolDefinition(TypedDict):
    """OpenAI function-tool envelope assembled for an LLM tool catalog."""

    type: Literal["function"]
    function: ToolFunctionDefinition


class ToolConfigField(TypedDict, total=False):
    """Known configuration-field attributes consumed by tool configuration code."""

    type: NotRequired[str]
    key: NotRequired[str]


class ToolConfigSchema(TypedDict, total=False):
    """UI configuration schema with optional typed field definitions."""

    fields: NotRequired[list[ToolConfigField]]
