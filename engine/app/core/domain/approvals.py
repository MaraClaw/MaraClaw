"""Approval commands and persisted state."""

from enum import StrEnum


class ApprovalAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
