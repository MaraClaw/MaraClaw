"""Shared organization sync data transfer objects."""

from dataclasses import dataclass, field

type ExternalProviderPayloadValue = (
    str | int | float | bool | list[ExternalProviderPayloadValue] | dict[str, ExternalProviderPayloadValue] | None
)
type ExternalProviderPayload = dict[str, ExternalProviderPayloadValue]


@dataclass
class ExternalDepartment:
    """Standardized department info from external providers."""

    external_id: str
    name: str
    parent_external_id: str | None = None
    member_count: int = 0
    raw_data: ExternalProviderPayload = field(default_factory=dict[str, ExternalProviderPayloadValue])


@dataclass
class ExternalUser:
    """Standardized user info from external providers."""

    external_id: str  # The unique, platform-stable ID (e.g., userid)
    name: str
    open_id: str = ""  # OAuth open_id
    unionid: str = ""  # Union ID for cross-app identification
    email: str = ""
    avatar_url: str = ""
    title: str = ""
    department_external_id: str = ""
    department_path: str = ""
    department_ids: list[str] = field(default_factory=list[str])  # List of dept IDs from provider
    mobile: str = ""
    status: str = "active"
    raw_data: ExternalProviderPayload = field(default_factory=dict[str, ExternalProviderPayloadValue])
