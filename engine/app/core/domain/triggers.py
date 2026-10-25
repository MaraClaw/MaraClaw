"""Durable trigger execution state."""

from enum import StrEnum


class TriggerExecutionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
