"""Key-result progress classification."""

from enum import StrEnum


class PeriodFrequency(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    CUSTOM = "custom"


class KeyResultStatus(StrEnum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BEHIND = "behind"
    COMPLETED = "completed"
