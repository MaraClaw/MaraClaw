from typing import Annotated, NotRequired

from fastapi.encoders import jsonable_encoder
from pydantic import PlainSerializer
from typing_extensions import TypedDict


class AgentActivityResponse(TypedDict):
    id: str
    action_type: str
    summary: str
    detail: Annotated[
        dict[str, object], PlainSerializer(jsonable_encoder, return_type=dict[str, object], when_used="json")
    ]
    related_id: str | None
    created_at: str | None


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
