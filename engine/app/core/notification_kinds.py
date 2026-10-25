"""Canonical notification kinds shared by persistence and HTTP contracts."""

from enum import StrEnum


class NotificationKind(StrEnum):
    MENTION = "mention"
    PLAZA_REPLY = "plaza_reply"
    PLAZA_COMMENT = "plaza_comment"
    BROADCAST = "broadcast"
    APPROVAL_PENDING = "approval_pending"
    APPROVAL_RESOLVED = "approval_resolved"
    AUTONOMY_L2 = "autonomy_l2"
    COMPANY_ADMISSION_PENDING = "company_admission_pending"
    COMPANY_ADMISSION_RESOLVED = "company_admission_resolved"
    COMPANY_ADMISSION_CREATED = "company_admission_created"
    AGENT_DELETION_REQUESTED = "agent_deletion_requested"
    AGENT_DELETION_RESOLVED = "agent_deletion_resolved"
    AGENT_OWNERSHIP_TRANSFERRED = "agent_ownership_transferred"
    ORGANIZATION_WELCOME = "organization_welcome"
    SYSTEM_ALERT = "system_alert"
    SYSTEM = "system"
    OPENCLAW_UPDATE = "openclaw_update"
