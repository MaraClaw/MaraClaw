from base64 import b64decode, urlsafe_b64encode
from typing import Annotated, ClassVar, Literal, NotRequired, Self
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from pydantic import AwareDatetime, BaseModel, ConfigDict, PlainSerializer
from typing_extensions import TypedDict

from app.core.domain.activity import ActivityAction


class AgentActivityResponse(TypedDict):
    id: str
    action_type: ActivityAction
    summary: str
    detail: Annotated[
        dict[str, object], PlainSerializer(jsonable_encoder, return_type=dict[str, object], when_used="json")
    ]
    related_id: str | None
    created_at: str | None


class AgentActivityTimelineItem(TypedDict):
    id: str
    action_type: str
    summary: str
    created_at: str | None


class AgentActivityTimelinePage(TypedDict):
    items: list[AgentActivityTimelineItem]
    next_cursor: str | None


class AgentActivityCursor(BaseModel):
    """Keyset position bound to one agent and one action-type filter."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    agent_id: UUID
    action_type: str | None
    created_at: AwareDatetime | None
    id: UUID

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> Self:
        """Parse an opaque cursor; any malformed input raises ``ValueError``."""
        payload = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        return cls.model_validate_json(payload, strict=True)


class AgentConversationResponse(TypedDict):
    conv_id: str
    partner_type: str
    partner_id: str
    partner_name: str
    last_message: str
    message_count: int
    last_at: str | None


class ConversationMessageResponse(TypedDict):
    id: str
    role: str
    content: str
    created_at: str | None
    sender_name: NotRequired[str | None]
