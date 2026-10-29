"""Digital employee state and distinct permission concepts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class AgentStatus(StrEnum):
    CREATING = "creating"
    RUNNING = "running"
    IDLE = "idle"
    STOPPED = "stopped"
    ERROR = "error"


class AgentType(StrEnum):
    OPENCLAW = "openclaw"


class PermissionAccessLevel(StrEnum):
    USE = "use"
    MANAGE = "manage"


class AgentAccessReason(StrEnum):
    CREATOR = "creator"
    ADMINISTRATOR = "administrator"
    COMPANY = "company"
    DIRECT = "direct"
    DEPARTMENT = "department"


@dataclass(frozen=True, slots=True)
class AgentAccessDecision:
    level: PermissionAccessLevel
    reason: AgentAccessReason


# Sealed transferred agents retain the existing explicit no-company-access sentinel.
type CompanyAccessLevel = PermissionAccessLevel | Literal[""]


class AgentSharingMode(StrEnum):
    COMPANY = "company"
    PRIVATE = "private"
    CUSTOM = "custom"


class PermissionGrantScope(StrEnum):
    COMPANY = "company"
    DEPARTMENT = "department"
    USER = "user"


class AgentPermissionScope(StrEnum):
    """Legacy HTTP sharing selector: user means private, not a grant scope."""

    COMPANY = "company"
    USER = "user"
    CUSTOM = "custom"
