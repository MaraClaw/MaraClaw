"""Audited HTTP owners and concrete non-route wire types (never copied values)."""

from enum import IntEnum, StrEnum

from pydantic import JsonValue, TypeAdapter

from app.api.agent_credentials import AgentCredentialPayload
from app.api.files import list_files_page
from app.api.skills import install_from_clawhub, refresh_registry_skill
from app.api.upload import UploadPolicy
from app.core.domain.activity import ActivityAction
from app.core.domain.agents import (
    AgentAccessReason,
    AgentPermissionScope,
    AgentSharingMode,
    AgentStatus,
    AgentType,
    PermissionAccessLevel,
    PermissionGrantScope,
)
from app.core.domain.approvals import ApprovalAction, ApprovalStatus
from app.core.domain.channels import ChannelType
from app.core.domain.control import ControlEnvironment, ControlStatus
from app.core.domain.focus import FocusKind, FocusStatus
from app.core.domain.notifications import BroadcastJobStatus
from app.core.domain.okr import KeyResultStatus
from app.core.domain.tasks import TaskPriority, TaskStatus, TaskType
from app.core.domain.triggers import TriggerExecutionStatus
from app.core.domain.users import UserRole
from app.core.notification_kinds import NotificationKind
from app.records.agent_deletion_request import CancelReason, DeletionStatus
from app.records.agent_resource_pressure_episode import PressureKind, PressureResource
from app.records.capacity_order import CapacityOrderKind, CapacityOrderStatus
from app.records.company_admission import AdmissionMethod, AdmissionStatus
from app.records.company_transfer import TransferChoice, TransferStatus
from app.records.organization_plan import (
    OrganizationAdditionalAccountMonthlyUsd,
    OrganizationAdditionalAgentMonthlyUsd,
    OrganizationAgentLimit,
    OrganizationAnnualPriceUsd,
    OrganizationCurrency,
    OrganizationDailyAgentMessageLimit,
    OrganizationFileLimitBytes,
    OrganizationGuestCpuShares,
    OrganizationGuestMemoryBytes,
    OrganizationGuestMillicpus,
    OrganizationGuestPidsLimit,
    OrganizationIncludedAgents,
    OrganizationPlanId,
    OrganizationPlanName,
    OrganizationStorageLimitBytes,
    OrganizationWorkspaceDiskBytes,
)
from app.schemas.chat_wire import CHAT_SERVER_EVENT_ADAPTER, ChatEventType, ChatRejectionReason
from app.schemas.schemas import ApprovalDecisionRequest

AUDITED_HTTP_MODULES = frozenset(
    f"app.api.{name}"
    for name in (
        "activity",
        "admin_capacity",
        "admin_resource_pressure",
        "admin_search_analytics",
        "advanced",
        "agent_credentials",
        "agent_deletion_requests",
        "agent_lifecycle",
        "agent_mcp",
        "agent_ownership_transfers",
        "agent_setup",
        "agent_setup_mcp",
        "agentbay_control",
        "agents",
        "auth",
        "capacity",
        "channel_catalog",
        "chat_sessions",
        "member_auth",
        "company_admissions",
        "departments",
        "focus",
        "notification",
        "okr",
        "onboarding",
        "organization",
        "organization_admissions",
        "relationships",
        "schedules",
        "tasks",
        "tools",
        "tool_catalog_import",
        "tool_config_operations",
        "tool_probes",
        "whatsapp",
        "whatsapp_personal",
        "triggers",
        "users",
        "feishu",
        "dingtalk",
        "wecom",
        "slack",
        "teams",
        "discord_bot",
        "google_chat",
        "wechat",
        "whatsapp",
    )
)

ENUM_TYPES: tuple[type[StrEnum | IntEnum], ...] = (
    ActivityAction,
    AgentAccessReason,
    AgentPermissionScope,
    AgentSharingMode,
    AgentStatus,
    AgentType,
    PermissionAccessLevel,
    PermissionGrantScope,
    ApprovalAction,
    ApprovalStatus,
    BroadcastJobStatus,
    ChannelType,
    ControlEnvironment,
    ControlStatus,
    FocusKind,
    FocusStatus,
    KeyResultStatus,
    TaskPriority,
    TaskStatus,
    TaskType,
    TriggerExecutionStatus,
    UserRole,
    NotificationKind,
    CancelReason,
    DeletionStatus,
    AdmissionMethod,
    AdmissionStatus,
    TransferChoice,
    TransferStatus,
    OrganizationPlanId,
    OrganizationPlanName,
    OrganizationAnnualPriceUsd,
    OrganizationAdditionalAccountMonthlyUsd,
    OrganizationCurrency,
    OrganizationAgentLimit,
    OrganizationIncludedAgents,
    OrganizationAdditionalAgentMonthlyUsd,
    OrganizationStorageLimitBytes,
    OrganizationFileLimitBytes,
    OrganizationDailyAgentMessageLimit,
    OrganizationGuestMemoryBytes,
    OrganizationGuestMillicpus,
    OrganizationGuestCpuShares,
    OrganizationGuestPidsLimit,
    OrganizationWorkspaceDiskBytes,
    PressureResource,
    PressureKind,
    CapacityOrderKind,
    CapacityOrderStatus,
    ChatRejectionReason,
    ChatEventType,
)

PUBLIC_SCHEMA_NAMES = {
    "JsonValue": "ToolCallValue",
    "pydantic__types__JsonValue": "JsonValue",
    "app__core__json_types__JsonValue-Output": "JsonOutputValue",
    "JsonValue-Input": "JsonInputValue",
    "JsonObject-Input": "JsonInputObject",
    "JsonObject-Output": "JsonOutputObject",
}

AUDITED_HTTP_ENDPOINTS = frozenset({list_files_page, install_from_clawhub, refresh_registry_skill})


def explicit_schemas() -> dict[str, JsonValue]:
    schemas: dict[str, JsonValue] = {
        enum_type.__name__: TypeAdapter(enum_type).json_schema(ref_template="#/components/schemas/{model}")
        for enum_type in ENUM_TYPES
    }
    schemas["ChatServerEvent"] = CHAT_SERVER_EVENT_ADAPTER.json_schema(ref_template="#/components/schemas/{model}")
    schemas["AgentCredentialPayload"] = TypeAdapter(AgentCredentialPayload).json_schema(
        ref_template="#/components/schemas/{model}"
    )
    schemas["ApprovalDecisionRequest"] = ApprovalDecisionRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    schemas["UploadPolicy"] = UploadPolicy.model_json_schema(ref_template="#/components/schemas/{model}")
    return schemas
