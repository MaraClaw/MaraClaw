"""Audited HTTP owners and concrete non-route wire types (never copied values)."""

from enum import StrEnum

from pydantic import JsonValue, TypeAdapter

from app.core.domain.activity import ActivityAction
from app.core.domain.agents import (
    AgentPermissionScope,
    AgentSharingMode,
    AgentStatus,
    PermissionAccessLevel,
    PermissionGrantScope,
)
from app.core.domain.approvals import ApprovalAction, ApprovalStatus
from app.core.domain.channels import ChannelType
from app.core.domain.control import ControlEnvironment, ControlStatus
from app.core.domain.focus import FocusKind, FocusStatus
from app.core.domain.okr import KeyResultStatus
from app.core.domain.tasks import TaskPriority, TaskStatus, TaskType
from app.core.domain.triggers import TriggerExecutionStatus
from app.core.domain.users import UserRole
from app.core.notification_kinds import NotificationKind
from app.records.agent_deletion_request import CancelReason, DeletionStatus
from app.records.company_admission import AdmissionMethod, AdmissionStatus
from app.records.company_transfer import TransferChoice, TransferStatus
from app.schemas.chat_wire import CHAT_SERVER_EVENT_ADAPTER, ChatEventType, ChatRejectionReason
from app.schemas.schemas import ApprovalDecisionRequest

AUDITED_HTTP_MODULES = frozenset(
    f"app.api.{name}"
    for name in (
        "activity",
        "admin_search_analytics",
        "advanced",
        "agent_deletion_requests",
        "agent_ownership_transfers",
        "agentbay_control",
        "agents",
        "auth",
        "member_auth",
        "company_admissions",
        "focus",
        "notification",
        "okr",
        "onboarding",
        "tasks",
        "tools",
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

ENUM_TYPES: tuple[type[StrEnum], ...] = (
    ActivityAction,
    AgentPermissionScope,
    AgentSharingMode,
    AgentStatus,
    PermissionAccessLevel,
    PermissionGrantScope,
    ApprovalAction,
    ApprovalStatus,
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


def explicit_schemas() -> dict[str, JsonValue]:
    schemas: dict[str, JsonValue] = {
        enum_type.__name__: TypeAdapter(enum_type).json_schema(ref_template="#/components/schemas/{model}")
        for enum_type in ENUM_TYPES
    }
    schemas["ChatServerEvent"] = CHAT_SERVER_EVENT_ADAPTER.json_schema(ref_template="#/components/schemas/{model}")
    schemas["ApprovalDecisionRequest"] = ApprovalDecisionRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    return schemas
