"""Dictionary contracts for OKR coordination responses."""

from typing import Literal, NotRequired, TypedDict


class MemberWithoutOkr(TypedDict):
    id: str
    type: str
    display_name: str
    avatar_url: str
    channel: str | None
    channel_user_id: str | None
    source_label: NotRequired[str | None]


class ChannelWarning(TypedDict):
    channel_type: str
    channel_display: str
    affected_members: list[str]
    count: int


class OutreachError(TypedDict):
    message: str
    timestamp: str
    is_read: bool


class MembersWithoutOkrResponse(TypedDict):
    period_start: str
    period_end: str
    company_okr_exists: bool
    okr_agent_id: str | None
    members_without_okr: list[MemberWithoutOkr]
    tracked_user_ids: list[str]
    tracked_agent_ids: list[str]
    total: int
    last_outreach_error: OutreachError | None
    channel_warnings: list[ChannelWarning]


class OutreachNoActionResponse(TypedDict):
    status: Literal["no_action"]
    message: str
    okr_agent_id: str


class OutreachAcceptedResponse(TypedDict):
    status: Literal["accepted"]
    message: str
    okr_agent_id: str
    members_count: int


type MemberOutreachResponse = OutreachNoActionResponse | OutreachAcceptedResponse


class DailyCollectionResponse(TypedDict):
    status: Literal["no_action", "accepted"]
    message: str
    okr_agent_id: str
    member_count: int


class OkrSyncResponse(TypedDict):
    status: Literal["ok"]
    okr_agent_id: str


class OkrDeleteResponse(TypedDict):
    status: Literal["success"]
