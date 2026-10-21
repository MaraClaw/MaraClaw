from datetime import datetime
from typing import Literal, NotRequired
from uuid import UUID

from typing_extensions import TypedDict

from app.core.json_types import JsonObject, JsonValue
from app.services.system_email_service import EmailTemplates


class EmailExistsResponse(TypedDict):
    exists: bool


class LLMProbeResponse(TypedDict):
    success: bool
    latency_ms: int
    reply: NotRequired[str]
    error: NotRequired[str]


class EnterpriseStatsResponse(TypedDict):
    total_agents: int
    running_agents: int
    total_users: int
    pending_approvals: int


class EmptyTenantQuotas(TypedDict):
    pass


class TenantQuotasResponse(TypedDict):
    default_message_limit: int
    default_message_period: str
    default_max_agents: int
    default_agent_ttl_hours: int
    default_max_llm_calls_per_day: int
    min_heartbeat_interval_minutes: int
    default_max_triggers: int
    min_poll_interval_floor: int
    max_webhook_rate_ceiling: int


class TenantQuotasUpdatedResponse(TypedDict):
    message: str
    heartbeat_agents_adjusted: int


class EmailSuccessResponse(TypedDict):
    success: Literal[True]
    message: str


class EmailTemplatesResponse(TypedDict):
    templates: EmailTemplates
    variables: dict[str, list[str]]
    defaults: EmailTemplates


class NotificationBarResponse(TypedDict):
    enabled: JsonValue
    text: JsonValue
    updated_at: str | None


class SystemSettingResponse(TypedDict):
    key: str
    value: JsonObject
    updated_at: NotRequired[str | None]


class IdentityProviderPayload(TypedDict):
    id: UUID
    provider_type: str
    name: str
    is_active: bool
    sso_login_enabled: bool
    config: JsonObject | None
    tenant_id: UUID | None
    updated_at: datetime | None
    created_at: datetime
    sso_domain: str | None


class InvitationCodesCreatedResponse(TypedDict):
    created: int
    codes: list[str]


class UsersInvitedResponse(TypedDict):
    invited: int
    message: str


class InvitationCodePayload(TypedDict):
    id: str
    code: str
    max_uses: int
    used_count: int
    is_active: bool
    created_at: str | None


class InvitationCodesResponse(TypedDict):
    items: list[InvitationCodePayload]
    total: int
    page: int
    page_size: int


class InvitationDeactivatedResponse(TypedDict):
    status: Literal["deactivated"]
