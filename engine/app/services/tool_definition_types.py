"""Shared builtin tool seed envelope without catalog or seeder dependencies."""

from typing import NotRequired, TypedDict

from typing_extensions import TypedDict as ExtensibleTypedDict

from app.core.json_types import JsonObject, JsonValue
from app.core.tool_types import ToolConfigField, ToolConfigSchema, ToolParameterSchema


class BuiltinToolDefinition(TypedDict):
    name: str
    display_name: str
    description: str
    category: str
    icon: str
    is_default: bool
    parameters_schema: ToolParameterSchema
    config: JsonObject
    config_schema: ToolConfigSchema


class BuiltinToolConfigField(ExtensibleTypedDict, extra_items=JsonValue):
    key: NotRequired[str]
    type: NotRequired[str]


def builtin_tool_config_fields(*fields: BuiltinToolConfigField) -> list[ToolConfigField]:
    return list(fields)
