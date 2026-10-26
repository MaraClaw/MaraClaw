"""Dictionary response contracts for agent management."""

from copy import deepcopy
from datetime import datetime
from typing import Literal, NotRequired, TypedDict
from uuid import UUID

from app.core.domain.agents import (
    AgentAccessReason,
    AgentSharingMode,
    AgentStatus,
    CompanyAccessLevel,
    PermissionAccessLevel,
)
from app.core.domain.approvals import ApprovalStatus
from app.core.domain.users import UserRole
from app.core.json_types import JsonObject
from app.schemas.schemas import AgentOut


class AgentTemplateResponse(TypedDict):
    id: str
    name: str
    description: str
    icon: str
    category: str
    is_builtin: bool
    soul_template: str
    default_skills: list[str]
    default_autonomy_policy: JsonObject
    capability_bullets: list[str]
    has_bootstrap: bool


class AgentModelDump(TypedDict):
    """Python-mode dictionary produced by the existing AgentOut model."""

    id: UUID
    name: str
    avatar_url: str | None
    role_description: str
    bio: str | None
    welcome_message: str | None
    status: AgentStatus
    creator_id: UUID
    creator_username: str | None
    primary_model_id: UUID | None
    secondary_model_id: UUID | None
    fallback_model_id: UUID | None
    autonomy_policy: JsonObject
    tokens_used_today: int
    tokens_used_month: int
    tokens_used_total: int
    cache_read_tokens_today: int
    cache_read_tokens_month: int
    cache_read_tokens_total: int
    cache_creation_tokens_today: int
    cache_creation_tokens_month: int
    cache_creation_tokens_total: int
    max_tokens_per_day: int | None
    max_tokens_per_month: int | None
    context_window_size: int
    max_tool_rounds: int
    max_triggers: int
    min_poll_interval_min: int
    webhook_rate_limit: int
    heartbeat_enabled: bool
    heartbeat_interval_minutes: int
    heartbeat_active_hours: str
    last_heartbeat_at: datetime | None
    timezone: str | None
    expires_at: datetime | None
    is_expired: bool
    is_system: bool
    access_mode: AgentSharingMode
    company_access_level: CompanyAccessLevel
    access_reason: AgentAccessReason | None
    llm_calls_today: int
    max_llm_calls_per_day: int
    agent_type: str
    gogcli_enabled: bool
    openclaw_last_seen: datetime | None
    unread_count: int
    has_api_key: bool
    onboarded_for_me: bool
    created_at: datetime
    last_active_at: datetime | None


def agent_model_dump(model: AgentOut) -> AgentModelDump:
    return {
        "id": model.id,
        "name": model.name,
        "avatar_url": model.avatar_url,
        "role_description": model.role_description,
        "bio": model.bio,
        "welcome_message": model.welcome_message,
        "status": model.status,
        "creator_id": model.creator_id,
        "creator_username": model.creator_username,
        "primary_model_id": model.primary_model_id,
        "secondary_model_id": model.secondary_model_id,
        "fallback_model_id": model.fallback_model_id,
        "autonomy_policy": deepcopy(model.autonomy_policy),
        "tokens_used_today": model.tokens_used_today,
        "tokens_used_month": model.tokens_used_month,
        "tokens_used_total": model.tokens_used_total,
        "cache_read_tokens_today": model.cache_read_tokens_today,
        "cache_read_tokens_month": model.cache_read_tokens_month,
        "cache_read_tokens_total": model.cache_read_tokens_total,
        "cache_creation_tokens_today": model.cache_creation_tokens_today,
        "cache_creation_tokens_month": model.cache_creation_tokens_month,
        "cache_creation_tokens_total": model.cache_creation_tokens_total,
        "max_tokens_per_day": model.max_tokens_per_day,
        "max_tokens_per_month": model.max_tokens_per_month,
        "context_window_size": model.context_window_size,
        "max_tool_rounds": model.max_tool_rounds,
        "max_triggers": model.max_triggers,
        "min_poll_interval_min": model.min_poll_interval_min,
        "webhook_rate_limit": model.webhook_rate_limit,
        "heartbeat_enabled": model.heartbeat_enabled,
        "heartbeat_interval_minutes": model.heartbeat_interval_minutes,
        "heartbeat_active_hours": model.heartbeat_active_hours,
        "last_heartbeat_at": model.last_heartbeat_at,
        "timezone": model.timezone,
        "expires_at": model.expires_at,
        "is_expired": model.is_expired,
        "is_system": model.is_system,
        "access_mode": model.access_mode,
        "company_access_level": model.company_access_level,
        "access_reason": model.access_reason,
        "llm_calls_today": model.llm_calls_today,
        "max_llm_calls_per_day": model.max_llm_calls_per_day,
        "agent_type": model.agent_type,
        "gogcli_enabled": model.gogcli_enabled,
        "openclaw_last_seen": model.openclaw_last_seen,
        "unread_count": model.unread_count,
        "has_api_key": model.has_api_key,
        "onboarded_for_me": model.onboarded_for_me,
        "created_at": model.created_at,
        "last_active_at": model.last_active_at,
    }


class AgentCreatedResponse(AgentModelDump):
    api_key: str
    access_level: PermissionAccessLevel


class AgentDetailResponse(AgentModelDump):
    access_level: PermissionAccessLevel
    effective_timezone: str


class AgentClampedField(TypedDict):
    field: str
    requested: int
    applied: int
    reason: Literal["company_floor", "company_ceiling"]


class AgentUpdatedResponse(AgentModelDump):
    _clamped_fields: NotRequired[list[AgentClampedField]]


class PermissionScopeName(TypedDict):
    id: str
    name: str


class DepartmentAccess(PermissionScopeName):
    access_level: Literal["use"]


class PermissionDepartmentFields(TypedDict):
    department_ids: list[str]
    department_access: list[DepartmentAccess]


class PermissionUserAccess(PermissionScopeName):
    username: str
    email: str | None
    role: UserRole
    access_level: PermissionAccessLevel
    is_required: bool
    required_reason: Literal["creator", "company_admin"] | None


class AgentPermissionsResponse(PermissionDepartmentFields):
    scope_type: AgentSharingMode
    scope_ids: list[str]
    scope_names: NotRequired[list[PermissionScopeName]]
    user_access: list[PermissionUserAccess]
    access_level: PermissionAccessLevel
    effective_access_level: PermissionAccessLevel
    can_manage: bool
    is_owner: bool
    creator_id: str | None


class PermissionDepartmentsResponse(TypedDict):
    departments: list[PermissionScopeName]


class PermissionCandidate(PermissionScopeName):
    username: str
    email: str | None
    title: str | None
    avatar_url: str | None


class PermissionCandidatesResponse(TypedDict):
    users: list[PermissionCandidate]
    agents: list[PermissionCandidate]


class PermissionCandidatesWithoutTenantResponse(TypedDict):
    candidates: list[PermissionCandidate]


class AgentApprovalResponse(TypedDict):
    id: str
    agent_id: str
    action_type: str
    details: JsonObject
    status: ApprovalStatus
    created_at: str | None
    resolved_at: str | None
    resolved_by: str | None


class AgentApprovalResolvedResponse(TypedDict):
    id: str
    status: ApprovalStatus
    resolved_at: str | None


class AgentApiKeyResponse(TypedDict):
    api_key: str
    message: str


class AgentGatewayMessageResponse(TypedDict):
    id: str
    sender_agent_name: str | None
    content: str
    status: str
    result: str | None
    created_at: str | None
    delivered_at: str | None
    completed_at: str | None
