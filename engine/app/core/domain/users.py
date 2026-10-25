"""Tenant membership roles."""

from enum import StrEnum


class UserRole(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    ORG_ADMIN = "org_admin"
    AGENT_ADMIN = "agent_admin"
    MEMBER = "member"
