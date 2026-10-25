"""Structured focus state; source remains an extensible string."""

from enum import StrEnum


class FocusStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class FocusKind(StrEnum):
    NORMAL = "normal"
    SYSTEM = "system"
