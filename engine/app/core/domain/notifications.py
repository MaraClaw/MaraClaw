"""Persisted broadcast notification fanout state."""

from enum import StrEnum


class BroadcastJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
