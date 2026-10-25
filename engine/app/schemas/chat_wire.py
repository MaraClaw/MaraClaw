"""Engine-owned chat socket contract; importing this module starts no runtime services.

Receipt/error variants are closed. Progress events intentionally retain JSON-valued
extension fields from streaming/tool producers; their event tags are still closed.
"""

from enum import StrEnum
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Discriminator, Field, JsonValue, Tag, TypeAdapter, model_validator

from app.core.json_types import JsonObject


class ChatEventType(StrEnum):
    CONNECTED = "connected"
    TURN_ACCEPTED = "turn_accepted"
    TURN_REJECTED = "turn_rejected"
    CHUNK = "chunk"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    AGENTBAY_LIVE = "agentbay_live"
    WORKSPACE_DRAFT = "workspace_draft"
    STATUS = "status"
    INFO = "info"
    ERROR = "error"
    DONE = "done"
    ONBOARDED = "onboarded"
    TRIGGER_NOTIFICATION = "trigger_notification"


class ChatRejectionReason(StrEnum):
    QUOTA_EXCEEDED = "quota_exceeded"
    AGENT_EXPIRED = "agent_expired"
    NO_COMPANY_MODEL = "no_company_model"
    ENQUEUE_FAILED = "enqueue_failed"
    GUEST_CONFIG_FAILED = "guest_config_failed"
    INVALID_SUBMISSION_ID = "invalid_submission_id"
    SUBMISSION_CONFLICT = "submission_conflict"
    INVALID_ATTACHMENTS = "invalid_attachments"
    OPENCLAW_UPDATE_REQUIRED = "openclaw_update_required"
    OPENCLAW_UPDATE_IN_PROGRESS = "openclaw_update_in_progress"


type OnboardingRejectionReason = Literal[
    ChatRejectionReason.QUOTA_EXCEEDED,
    ChatRejectionReason.AGENT_EXPIRED,
    ChatRejectionReason.NO_COMPANY_MODEL,
    ChatRejectionReason.ENQUEUE_FAILED,
    ChatRejectionReason.GUEST_CONFIG_FAILED,
    ChatRejectionReason.OPENCLAW_UPDATE_REQUIRED,
    ChatRejectionReason.OPENCLAW_UPDATE_IN_PROGRESS,
]
type SubmissionRejectionReason = (
    OnboardingRejectionReason
    | Literal[ChatRejectionReason.SUBMISSION_CONFLICT, ChatRejectionReason.INVALID_ATTACHMENTS]
)


class _WireModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class _ChatEvent[EventTag: str](_WireModel):
    type: EventTag


class ConnectedEvent(_ChatEvent[Literal[ChatEventType.CONNECTED]]):
    session_id: UUID


class TurnAcceptedEvent(_ChatEvent[Literal[ChatEventType.TURN_ACCEPTED]]):
    client_submission_id: UUID
    message_id: UUID
    session_id: UUID


class OnboardingTurnRejectedEvent(_ChatEvent[Literal[ChatEventType.TURN_REJECTED]]):
    reason: OnboardingRejectionReason
    content: str
    notice_id: UUID | None = None


class CorrelatedTurnRejectedEvent(_ChatEvent[Literal[ChatEventType.TURN_REJECTED]]):
    client_submission_id: UUID
    reason: SubmissionRejectionReason
    content: str
    notice_id: UUID | None = None


class InvalidSubmissionRejectedEvent(_ChatEvent[Literal[ChatEventType.TURN_REJECTED]]):
    """An invalid UUID cannot identify a pending submission; never infer correlation."""

    reason: Literal[ChatRejectionReason.INVALID_SUBMISSION_ID]
    content: str


class StringErrorEvent(_ChatEvent[Literal[ChatEventType.ERROR]]):
    content: str


class ArchivedSessionDetail(_WireModel):
    reason: Literal["session_archived"]
    session_id: UUID


class ArchivedSessionErrorEvent(_ChatEvent[Literal[ChatEventType.ERROR]]):
    content: ArchivedSessionDetail


class ChatAttachmentReference(_WireModel):
    path: str = Field(min_length=1)
    name: str = Field(min_length=1)


class DoneEvent(_ChatEvent[Literal[ChatEventType.DONE]]):
    content: str | None = None
    role: str | None = None
    session_id: UUID | None = None
    message_id: UUID | None = None
    entry_id: UUID | None = None
    client_submission_id: UUID | None = None
    attachments: list[ChatAttachmentReference] | None = None


class OnboardedEvent(_ChatEvent[Literal[ChatEventType.ONBOARDED]]):
    agent_id: UUID


class TriggerNotificationEvent(_ChatEvent[Literal[ChatEventType.TRIGGER_NOTIFICATION]]):
    content: str
    triggers: list[str]
    session_id: UUID


_JSON_OBJECT_ADAPTER = TypeAdapter[JsonObject](JsonObject)


class _ProgressEvent[EventTag: str](_ChatEvent[EventTag]):
    """Only progress payload extensions are open, and only to JSON values."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="allow")
    content: str | None = None

    @model_validator(mode="before")
    @classmethod
    def json_payload(cls, value: object) -> JsonObject:
        return _JSON_OBJECT_ADAPTER.validate_python(value)


class TextProgressEvent(
    _ProgressEvent[Literal[ChatEventType.CHUNK, ChatEventType.THINKING, ChatEventType.STATUS, ChatEventType.INFO]]
):
    pass


class LivePreview(_WireModel):
    env: str | None = None
    screenshot_url: str | None = None
    output: str | None = None


class ToolCallEvent(_ProgressEvent[Literal[ChatEventType.TOOL_CALL]]):
    name: str | None = None
    status: str | None = None
    arguments: JsonValue = None
    result: JsonValue = None
    live_preview: LivePreview | None = None


class AgentBayLiveEvent(_ProgressEvent[Literal[ChatEventType.AGENTBAY_LIVE]]):
    env: str | None = None
    output: str | None = None
    live_preview: LivePreview | None = None


class WorkspaceDraftEvent(_ProgressEvent[Literal[ChatEventType.WORKSPACE_DRAFT]]):
    name: str | None = None


def _event_tag(value: object) -> str | None:
    if isinstance(value, dict):
        tag = value.get("type")
    elif isinstance(value, _ChatEvent):
        tag = value.type
    else:
        return None
    if tag in (ChatEventType.CHUNK, ChatEventType.THINKING, ChatEventType.STATUS, ChatEventType.INFO):
        return "text_progress"
    return tag if isinstance(tag, str) else None


type ChatServerEvent = Annotated[
    Annotated[ConnectedEvent, Tag(ChatEventType.CONNECTED)]
    | Annotated[TurnAcceptedEvent, Tag(ChatEventType.TURN_ACCEPTED)]
    | Annotated[
        CorrelatedTurnRejectedEvent | OnboardingTurnRejectedEvent | InvalidSubmissionRejectedEvent,
        Tag(ChatEventType.TURN_REJECTED),
    ]
    | Annotated[StringErrorEvent | ArchivedSessionErrorEvent, Tag(ChatEventType.ERROR)]
    | Annotated[DoneEvent, Tag(ChatEventType.DONE)]
    | Annotated[OnboardedEvent, Tag(ChatEventType.ONBOARDED)]
    | Annotated[TriggerNotificationEvent, Tag(ChatEventType.TRIGGER_NOTIFICATION)]
    | Annotated[TextProgressEvent, Tag("text_progress")]
    | Annotated[ToolCallEvent, Tag(ChatEventType.TOOL_CALL)]
    | Annotated[AgentBayLiveEvent, Tag(ChatEventType.AGENTBAY_LIVE)]
    | Annotated[WorkspaceDraftEvent, Tag(ChatEventType.WORKSPACE_DRAFT)],
    Discriminator(_event_tag),
]

CHAT_SERVER_EVENT_ADAPTER = TypeAdapter(ChatServerEvent)


def serialize_chat_server_event(value: object) -> JsonObject:
    """Validate outbound JSON without rewriting UUID spelling, nulls or omitted fields."""
    payload = _JSON_OBJECT_ADAPTER.validate_python(value)
    _ = CHAT_SERVER_EVENT_ADAPTER.validate_python(payload)
    return payload
