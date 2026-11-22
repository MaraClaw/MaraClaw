"""Tenant membership roles and roster limits."""

from enum import StrEnum
from typing import Final

MAX_USERS_PAGE_SIZE: Final = 100


class UserRole(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    ORG_ADMIN = "org_admin"
    AGENT_ADMIN = "agent_admin"
    MEMBER = "member"
