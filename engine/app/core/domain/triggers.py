"""Durable trigger execution state."""

from enum import StrEnum

TRIGGER_DISABLED_ERROR = "Execution cancelled because its trigger was disabled before it started"
"""Recorded on the pending executions a disable settles; they never run, even if the trigger is enabled again."""


class TriggerExecutionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
