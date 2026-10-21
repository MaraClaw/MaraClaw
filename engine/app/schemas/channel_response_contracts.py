from typing import Literal

from typing_extensions import TypedDict

from app.core.json_types import JsonValue


class StatusAcknowledgement(TypedDict):
    status: Literal["ok"]


class GatewayHeartbeatResponse(StatusAcknowledgement):
    agent_id: str


class GatewayAgentMessageResponse(TypedDict):
    status: Literal["accepted"]
    target: str
    type: Literal["openclaw_agent"]
    message: str


class GatewayHumanMessageResponse(TypedDict):
    status: Literal["sent"]
    target: str
    type: Literal["human"]
    channel: Literal["feishu"]


type GatewaySendMessageResponse = GatewayAgentMessageResponse | GatewayHumanMessageResponse


class GatewaySetupGuideResponse(TypedDict):
    skill_filename: str
    skill_content: str
    heartbeat_addition: str


class MessageInboxItem(TypedDict):
    id: str
    sender_type: Literal["agent"]
    sender_name: str
    content: str
    session_title: str
    created_at: str | None


class WebhookUrlResponse(TypedDict):
    webhook_url: str


class DiscordPongResponse(TypedDict):
    type: Literal[1]


class DiscordDeferredResponse(TypedDict):
    type: Literal[5]


class DiscordMessageContent(TypedDict):
    content: str


class DiscordMessageResponse(TypedDict):
    type: Literal[4]
    data: DiscordMessageContent


type DiscordInteractionResponse = DiscordPongResponse | DiscordDeferredResponse | DiscordMessageResponse


class FeishuChallengeResponse(TypedDict):
    challenge: str


class FeishuEventResponse(TypedDict):
    code: Literal[0, 1]
    msg: str


class SlackChallengeResponse(TypedDict):
    challenge: JsonValue


class ScheduleTriggerResponse(TypedDict):
    status: Literal["triggered"]
    schedule_id: str


class TaskTriggerResponse(TypedDict):
    status: Literal["triggered"]
    task_id: str


class UserRoleResponse(StatusAcknowledgement):
    user_id: str
    role: str
