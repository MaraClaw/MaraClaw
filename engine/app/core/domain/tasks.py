"""Task classification, workflow, and urgency."""

from enum import StrEnum


class TaskType(StrEnum):
    TODO = "todo"
    SUPERVISION = "supervision"


class TaskStatus(StrEnum):
    PENDING = "pending"
    DOING = "doing"
    DONE = "done"


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"
