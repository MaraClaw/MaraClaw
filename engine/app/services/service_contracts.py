"""Fixed service-produced fragments and consumed template metadata."""

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from app.core.json_types import JsonObject, JsonValue


class OpenClawGatewayAuth(TypedDict, closed=True):
    mode: Literal["token"]
    token: str


class OpenClawGateway(TypedDict, closed=True):
    mode: Literal["local"]
    bind: Literal["lan"]
    port: Literal[18789]
    auth: OpenClawGatewayAuth


class OpenClawHooks(TypedDict, closed=True):
    enabled: Literal[True]
    path: Literal["/hooks"]
    token: str
    allowRequestSessionKey: Literal[True]
    allowedSessionKeyPrefixes: list[str]


class OpenClawInboxConfig(TypedDict, closed=True):
    gateway: OpenClawGateway
    hooks: OpenClawHooks


class OpenClawHeartbeat(TypedDict, closed=True):
    every: Literal["30s"]
    prompt: str


class WeChatContextEntry(TypedDict, closed=True):
    context_token: str
    conv_id: str
    updated_at: str


class TemplateMetadata(TypedDict, extra_items=JsonValue):
    name: str
    description: str
    icon: str
    category: str
    capability_bullets: NotRequired[list[str]]
    default_skills: NotRequired[list[str]]
    default_mcp_servers: NotRequired[list[str]]
    default_autonomy_policy: NotRequired[JsonObject]


class TemplateSeed(TypedDict):
    name: str
    description: str
    icon: str
    category: str
    is_builtin: bool
    capability_bullets: list[str]
    bootstrap_content: str | None
    soul_template: str
    default_skills: list[str]
    default_autonomy_policy: JsonObject
    default_mcp_servers: NotRequired[list[str]]


class TenantToolSettings(TypedDict, closed=True):
    config: JsonObject


class GoogleChatThread(TypedDict, closed=True):
    name: str


class GoogleChatMessage(TypedDict, closed=True):
    text: str
    thread: NotRequired[GoogleChatThread]


class TeamsConversation(TypedDict, closed=True):
    id: str


class TeamsMessageActivity(TypedDict, closed=True):
    type: Literal["message"]
    text: str
    conversation: TeamsConversation
